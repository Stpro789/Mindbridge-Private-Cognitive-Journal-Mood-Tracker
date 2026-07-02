import re
import os
import sys
import json
import datetime
from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import McpToolset, AgentTool
from google.adk.tools.mcp_tool import StdioConnectionParams
from google.adk.workflow import Workflow, node, Edge
from google.adk.events import RequestInput
from google.adk import Context
from google.genai import types

from app.config import config

# Import save_journal_entry directly to allow programmatic saving
from app.mcp_server import save_journal_entry

# Initialize Model
model = Gemini(
    model=config.model,
    retry_options=types.HttpRetryOptions(attempts=3),
)

# Initialize MCP Toolset
mcp_server_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")
from mcp import StdioServerParameters

mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[mcp_server_path],
            env={"PATH": os.environ.get("PATH", "")}
        )
    )
)

# 1. Specialized sub-agents
reflection_agent = LlmAgent(
    name="reflection_agent",
    model=model,
    instruction="You are the Mindbridge Reflection Agent. Analyze the user's thoughts and extract the core cognitive themes, patterns, and insights. Keep everything private and secure.",
    tools=[mcp_toolset]
)

mood_tracker_agent = LlmAgent(
    name="mood_tracker_agent",
    model=model,
    instruction="You are the Mindbridge Mood Tracker Agent. Analyze the emotional tone and content of the user's text. You MUST output a structured mood rating between 1 (extremely low) and 10 (extremely high) followed by a short explanation.",
    tools=[mcp_toolset]
)

# 2. Coordinator Agent
orchestrator_agent = LlmAgent(
    name="orchestrator_agent",
    model=model,
    instruction="You are the Mindbridge Coordinator. You synthesize the outputs of reflection_agent (cognitive themes) and mood_tracker_agent (mood score) into a cohesive, private daily journal report for the user. Summarize the key insights clearly.",
    tools=[AgentTool(reflection_agent), AgentTool(mood_tracker_agent)]
)

# State Schema
class MindbridgeState(BaseModel):
    raw_input: str = ""
    clean_text: str = ""
    reflection_draft: str = ""
    mood_rating: int = 5
    themes: list[str] = []
    approved: bool = False
    final_report: str = ""

# Security Utilities
def scrub_pii(text: str) -> str:
    # Email regex
    text = re.sub(r"[\w\.-]+@[\w\.-]+\.\w+", "[EMAIL_REDACTED]", text)
    # Phone regex
    text = re.sub(
        r"\+?\d{1,4}?[-.\s]?\(?\d{1,3}?\)?[-.\s]?\d{1,4}[-.\s]?\d{1,4}[-.\s]?\d{1,9}",
        "[PHONE_REDACTED]",
        text
    )
    return text

def detect_injection(text: str) -> bool:
    forbidden = [
        "ignore previous instructions",
        "system prompt",
        "you must",
        "override",
        "ignore rules",
        "prompt injection"
    ]
    for word in forbidden:
        if word in text.lower():
            return True
    return False

def check_consent(text: str) -> bool:
    if "opt out" in text.lower() or "do not store" in text.lower():
        return False
    return True

def write_audit_log(node_name: str, severity: str, message: str):
    log_entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "node": node_name,
        "severity": severity,
        "message": message
    }
    try:
        with open("audit.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception:
        pass

from typing import Optional

# 3. Workflow Function Nodes
@node
async def security_checkpoint(ctx: Context, node_input: Optional[str] = None) -> str:
    """Checks the raw input for prompt injections, scrubs PII, and validates consent."""
    node_input = node_input or ""
    ctx.state["raw_input"] = node_input
    
    if detect_injection(node_input):
        write_audit_log("security_checkpoint", "CRITICAL", "Prompt injection detected in input.")
        ctx.route = "SECURITY_EVENT"
        return "Security Alert: Forbidden input patterns detected."
        
    if not check_consent(node_input):
        write_audit_log("security_checkpoint", "WARNING", "Consent check failed: User requested not to store data.")
        ctx.route = "SECURITY_EVENT"
        return "Privacy Alert: Opt-out request detected. Processing aborted."
        
    # Scrub PII
    cleaned = scrub_pii(node_input)
    ctx.state["clean_text"] = cleaned
    write_audit_log("security_checkpoint", "INFO", "Input successfully cleared and scrubbed.")
    ctx.route = "CLEAN"
    return cleaned

@node
async def security_event_node(ctx: Context, node_input: Optional[str] = None) -> str:
    """Handles flagged security events by stopping processing."""
    node_input = node_input or ""
    return f"Security Alert: Your input could not be processed. Details: {node_input}"

@node(rerun_on_resume=True)
async def orchestrator_node(ctx: Context, node_input: Optional[str] = None) -> str:
    """Coordinates reflection and mood agents, then synthesizes a daily report draft."""
    node_input = node_input or ""
    # Run reflection sub-agent
    reflection_res = await ctx.run_node(reflection_agent, node_input)
    
    # Run mood sub-agent
    mood_res = await ctx.run_node(mood_tracker_agent, node_input)
    
    # Extract mood rating (1-10) using regex
    mood_match = re.search(r"\b([1-9]|10)\b", mood_res)
    mood_rating = int(mood_match.group(1)) if mood_match else 5
    
    # Extract themes (simple list of words starting with '#' or bullet points)
    themes = re.findall(r"#(\w+)", reflection_res)
    if not themes:
        themes = ["general-reflection"]
        
    ctx.state["mood_rating"] = mood_rating
    ctx.state["themes"] = themes
    ctx.state["reflection_draft"] = reflection_res
    
    # Request orchestrator synthesis
    orchestrator_input = (
        f"Cleaned journal entry: {node_input}\n"
        f"Themes/Insights: {reflection_res}\n"
        f"Mood score: {mood_rating}/10 ({mood_res})"
    )
    synthesis = await ctx.run_node(orchestrator_agent, orchestrator_input)
    ctx.state["final_report"] = synthesis
    return synthesis

@node(rerun_on_resume=True)
async def hitl_check_node(ctx: Context, node_input: Optional[str] = None):
    """Checks if mood rating requires human confirmation before saving."""
    node_input = node_input or ""
    mood_rating = ctx.state.get("mood_rating", 5)
    
    # Request review if mood is extreme (<= 3 or >= 9)
    if mood_rating <= 3 or mood_rating >= 9:
        user_response = ctx.resume_inputs.get("approval")
        if user_response is None:
            # Yield RequestInput to pause and wait for user approval
            yield RequestInput(
                interrupt_id="approval",
                message=(
                    f"Warning: Extreme mood detected ({mood_rating}/10). "
                    "Please review and approve this cognitive reflection draft before it is saved."
                ),
                payload={"draft": node_input}
            )
            return
        
        # User responded, evaluate response
        if "yes" in str(user_response).lower() or "approve" in str(user_response).lower():
            ctx.state["approved"] = True
            write_audit_log("hitl_check_node", "INFO", "Reflection draft approved by user.")
        else:
            ctx.state["approved"] = False
            write_audit_log("hitl_check_node", "WARNING", "Reflection draft denied by user.")
    else:
        # Moderate mood is auto-approved
        ctx.state["approved"] = True
        write_audit_log("hitl_check_node", "INFO", "Reflection draft auto-approved.")

@node
async def final_output_node(ctx: Context, node_input: Optional[str] = None) -> str:
    """Saves approved reflections using the MCP tool and prints the final report."""
    node_input = node_input or ""
    approved = ctx.state.get("approved", False)
    if approved:
        # Programmatically trigger the save tool
        save_res = save_journal_entry(
            content=ctx.state.get("clean_text", ""),
            mood_rating=ctx.state.get("mood_rating", 5),
            themes=ctx.state.get("themes", ["general-reflection"])
        )
        return (
            f"### Mindbridge Journal Saved!\n\n"
            f"**Mood Rating:** {ctx.state.get('mood_rating')}/10\n"
            f"**Themes:** {', '.join(ctx.state.get('themes', []))}\n\n"
            f"**Reflection:**\n{ctx.state.get('final_report')}\n\n"
            f"*{save_res}*"
        )
    else:
        return "### Mindbridge Journal Discarded\n\nJournal entry was not approved and has not been saved."


# 4. Construct Workflow Graph
workflow = Workflow(
    name="mindbridge_workflow",
    edges=[
        ("START", security_checkpoint),
        Edge(from_node=security_checkpoint, to_node=security_event_node, route="SECURITY_EVENT"),
        Edge(from_node=security_checkpoint, to_node=orchestrator_node, route="CLEAN"),
        (orchestrator_node, hitl_check_node),
        (hitl_check_node, final_output_node)
    ],
    state_schema=MindbridgeState
)

# Root App export
root_agent = workflow
app = App(
    root_agent=root_agent,
    name="app"
)
