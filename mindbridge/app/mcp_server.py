import os
import json
import datetime
from mcp.server.fastmcp import FastMCP

# Initialize the FastMCP server
mcp = FastMCP("mindbridge-mcp-server")

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "journals.json")

def _read_journals():
    if not os.path.exists(DB_FILE):
        return []
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def _write_journals(data):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False

@mcp.tool()
def save_journal_entry(content: str, mood_rating: int, themes: list[str]) -> str:
    """Save a processed journal entry with its content, mood rating, and themes.

    Args:
        content: The text content of the journal.
        mood_rating: Computed mood rating (1 to 10).
        themes: List of cognitive themes extracted from the journal.
    """
    journals = _read_journals()
    new_entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "content": content,
        "mood_rating": mood_rating,
        "themes": themes
    }
    journals.append(new_entry)
    if _write_journals(journals):
        return "Journal entry saved successfully."
    return "Error: Failed to save the journal entry."

@mcp.tool()
def get_mood_history() -> list[dict]:
    """Retrieve historical mood rating trends from past journal entries."""
    journals = _read_journals()
    history = []
    for entry in journals:
        history.append({
            "timestamp": entry.get("timestamp"),
            "mood_rating": entry.get("mood_rating")
        })
    return history

@mcp.tool()
def get_recent_reflections() -> list[dict]:
    """Retrieve details of the 5 most recent journal entries including themes and content."""
    journals = _read_journals()
    # Sort by timestamp descending
    journals_sorted = sorted(journals, key=lambda x: x.get("timestamp", ""), reverse=True)
    return journals_sorted[:5]

if __name__ == "__main__":
    mcp.run(transport="stdio")
