"""Per-session transcripts: what is written, who can read it, and what can't be read."""

from tests.conftest import ADMIN, Chat


def test_turn_is_recorded_with_tools_results_and_answer(api, chat):
    done = chat.ask("Who won IPL 2010?")
    r = api.get("/api/transcript", headers={"Authorization": f"Bearer {chat.token}"})
    assert r.status_code == 200
    text = r.json()["content"]
    assert "FAN: Who won IPL 2010?" in text
    assert "[tool]" in text and "season_summary" in text          # every tool call, with its arguments
    assert "[result] tool:season_summary" in text                 # and what it returned
    assert "ANSWER (" in text and done["answer"][:40] in text
    assert "PATH: fact" in text


def test_each_session_sees_only_its_own_transcript(api, chat):
    chat.ask("What is a free hit?")
    other = Chat(api)
    r = api.get("/api/transcript", headers={"Authorization": f"Bearer {other.token}"})
    assert r.json()["name"] is None and "free hit" not in r.json()["content"]
    assert api.get("/api/transcript").status_code == 401           # no token, no transcript


def test_personal_data_is_redacted(api):
    c = Chat(api)
    c.ask("my email is fan@example.com, who won IPL 2010?")
    text = api.get("/api/transcript", headers={"Authorization": f"Bearer {c.token}"}).json()["content"]
    assert "fan@example.com" not in text


def test_admin_can_list_and_read_but_not_escape_the_folder(api, chat):
    chat.ask("Who won IPL 2011?")
    assert api.get("/api/admin/transcripts").status_code in (401, 403)
    h = {"X-Admin-Token": ADMIN}
    files = api.get("/api/admin/transcripts", headers=h).json()["files"]
    assert files and files[0]["name"].endswith(".txt") and files[0]["turns"] >= 1
    assert "FAN:" in api.get(f"/api/admin/transcripts/{files[0]['name']}", headers=h).json()["content"]
    for bad in ("..%2F..%2F.env", "../cricket.sqlite3", "x.txt", "20260101-000000_zzzzzzzzzzzz.txt"):
        assert api.get(f"/api/admin/transcripts/{bad}", headers=h).status_code == 404


def test_session_id_never_appears_in_file_names(api, chat):
    from cricket_chatbot.security.auth import verify_token
    chat.ask("What is DRS?")
    sid = verify_token(api.app_state.settings.session_secret, chat.token, 3600)
    names = [f["name"] for f in api.get("/api/admin/transcripts", headers={"X-Admin-Token": ADMIN}).json()["files"]]
    assert sid and not any(sid in n for n in names)
