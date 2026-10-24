import json
import re
import time
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st
from google import genai

st.set_page_config(page_title="Outreach Assistant", page_icon="✈️", layout="wide")

MODEL = st.secrets.get("GEMINI_MODEL", "gemini-2.5-flash")

IG_MSG = (
    "Hi! I built an AI chatbot for travel agencies that answers package and visa questions "
    "instantly, even at night, and collects customer numbers for you. "
    "Live demo: https://travel-chatbot-demo.vercel.app/ "
    "Want me to set one up on your real packages for free?"
)


def wa_msg(name):
    return (
        f"Hi {name} team! I built an AI chatbot that answers customers' package and visa "
        "questions instantly, even at night, and collects their number for you. "
        "Live demo: https://travel-chatbot-demo.vercel.app/ "
        "Want me to set one up on your real packages for free?"
    )


# ---------- password gate ----------
def check_password():
    if st.session_state.get("authed"):
        return True
    pw = st.text_input("Password", type="password")
    if pw:
        if pw == st.secrets["APP_PASSWORD"]:
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("Wrong password")
    return False


if not check_password():
    st.stop()


# ---------- sheet calls ----------
def call_sheet(action, **kw):
    try:
        resp = requests.post(
            st.secrets["SHEET_URL"],
            json={"token": st.secrets["SHEET_TOKEN"], "action": action, **kw},
            timeout=90,
        )
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def refresh_board():
    res = call_sheet("status")
    if res.get("ok"):
        st.session_state.board = res
    else:
        st.session_state.board_error = res.get("error", "unknown error")


# ---------- file reading ----------
def file_to_text(f):
    name = f.name.lower()
    if name.endswith(".csv"):
        return pd.read_csv(f, dtype=str).fillna("").to_csv(index=False)
    if name.endswith((".xlsx", ".xls")):
        sheets = pd.read_excel(f, sheet_name=None, dtype=str)
        return "\n".join(df.fillna("").to_csv(index=False) for df in sheets.values())
    if name.endswith(".pdf"):
        import pdfplumber

        parts = []
        with pdfplumber.open(f) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        parts.append(" | ".join((c or "").replace("\n", " ") for c in row))
                if not tables:
                    parts.append(page.extract_text() or "")
        return "\n".join(parts)
    return f.read().decode("utf-8", "ignore")


def chunk_text(text, size=6000):
    chunks, cur = [], ""
    for line in text.splitlines():
        if len(cur) + len(line) > size and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


PROMPT = (
    "Extract travel agency records from the text below. The text may be CSV, a table, or plain text. "
    "Return ONLY a JSON array. Each item must be: "
    '{"business": "", "email": "", "whatsapp": "", "instagram": ""}. '
    "instagram may be a profile URL or a handle. "
    'Use "" when a value is missing or says NOT FOUND. Never invent or guess any value. '
    "Text:\n"
)

def generate_with_retry(client, contents):
    models = [MODEL]
    if "GEMINI_MODEL_2" in st.secrets:
        models.append(st.secrets["GEMINI_MODEL_2"])
    last = None
    for attempt in range(4):
        for m in models:
            try:
                return client.models.generate_content(
                    model=m,
                    contents=contents,
                    config={"response_mime_type": "application/json"},
                )
            except Exception as e:
                last = e
                if any(x in str(e) for x in ("503", "429", "UNAVAILABLE")):
                    continue
                raise
        time.sleep(4 * (attempt + 1))
    raise last
   
def extract_records(text):
    client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
    records = []
    for c in chunk_text(text):
               resp = generate_with_retry(client, PROMPT + c)
        try:
            data = json.loads(resp.text)
            if isinstance(data, list):
                records += [d for d in data if isinstance(d, dict)]
        except Exception:
            continue
    return records


def normalize_number(n):
    d = re.sub(r"\D", "", str(n or ""))
    if len(d) == 10:
        return "91" + d
    if 11 <= len(d) <= 13:
        return d
    return ""


def ig_user(v):
    v = str(v or "").strip()
    if not v or v.upper() == "NOT FOUND":
        return ""
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", v)
    u = m.group(1) if m else v.lstrip("@")
    if u.lower() in ("p", "reel", "reels", "explore", "accounts"):
        return ""
    return u if re.match(r"^[A-Za-z0-9._]{1,30}$", u) else ""


def clean(records):
    seen_e, seen_n, seen_i = set(), set(), set()
    rows, dups, invalid = [], 0, 0
    for r in records:
        biz = str(r.get("business", "")).strip()
        email = str(r.get("email", "")).strip().lower()
        num = normalize_number(r.get("whatsapp", ""))
        ig = ig_user(r.get("instagram", ""))
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
            email = ""
        if not (email or num or ig):
            invalid += 1
            continue
        if (email and email in seen_e) or (num and num in seen_n) or (ig and ig in seen_i):
            dups += 1
            continue
        if email:
            seen_e.add(email)
        if num:
            seen_n.add(num)
        if ig:
            seen_i.add(ig)
        rows.append({"business": biz or "there", "email": email, "whatsapp": num, "instagram": ig})
    return rows, dups, invalid


# ---------- chat helpers ----------
def say(role, text):
    st.session_state.messages.append((role, text))


def approve():
    p = st.session_state.pending
    if not p:
        return
    rows = p["rows"]
    res = call_sheet(
        "import",
        raw=[[r["business"], r["email"], r["whatsapp"], r["instagram"]] for r in rows],
        leads=[r["email"] for r in rows if r["email"]],
        whatsapp=[[r["business"], r["whatsapp"]] for r in rows if r["whatsapp"]],
        instagram=[[r["business"], r["instagram"]] for r in rows if r["instagram"]],
    )
    if not res.get("ok"):
        say("assistant", f"The sheet said: {res.get('error')}")
        return
    st.session_state.pending = None
    refresh_board()
    msg = (
        f"Done ✅ Old data was backed up and replaced.\n\n"
        f"- Emails: {res['leads']}\n- WhatsApp: {res['whatsapp']}\n- Instagram: {res['instagram']}\n\n"
        "Your WhatsApp and Instagram links are on the **Board** tab."
    )
    if res["leads"] > 0:
        msg += "\n\n**Start sending the emails right now?**"
        st.session_state.ask_start = True
    say("assistant", msg)


def cancel():
    st.session_state.pending = None
    say("assistant", "Okay, cancelled. Nothing was changed. Upload another file whenever you're ready.")


def start_emails():
    st.session_state.ask_start = False
    res = call_sheet("start")
    if res.get("ok"):
        say("assistant", f"Started 🚀 Sending {res['queued']} emails, one every 5 to 7 minutes. "
                         "You can close the app. Check the Board tab for progress.")
    else:
        say("assistant", f"Couldn't start: {res.get('error')}")
    refresh_board()


def skip_start():
    st.session_state.ask_start = False
    say("assistant", "Okay, emails are on hold. You can start them from the Board tab anytime.")


def mark(tab, row, key, idx):
    res = call_sheet("mark", tab=tab, row=row, value="Done")
    if res.get("ok"):
        st.session_state.board[key][idx]["done"] = True


def autoclear_if_finished():
    b = st.session_state.get("board")
    if not b:
        return
    e = b["email"]
    has_data = e["total"] > 0 or b["whatsapp"] or b["instagram"]
    email_done = e["total"] == 0 or (not e["running"] and e["sent"] + e["errors"] >= e["total"])
    links_done = all(x["done"] for x in b["whatsapp"]) and all(x["done"] for x in b["instagram"])
    if has_data and email_done and links_done:
        res = call_sheet("clear")
        if res.get("ok"):
            say("assistant", "🎉 Everything is finished, so I backed up and cleared the sheet. Ready for a new batch.")
            refresh_board()


# ---------- state ----------
if "messages" not in st.session_state:
    st.session_state.messages = [
        (
            "assistant",
            "Hey! 👋 What are we working on today? Upload your research file "
            "(CSV, Excel or PDF) and I'll sort it for you.",
        )
    ]
    st.session_state.pending = None
    st.session_state.ask_start = False
    st.session_state.done_files = set()
    st.session_state.board = None
    refresh_board()

st.title("✈️ Outreach Assistant")
tab_chat, tab_board = st.tabs(["💬 Chat", "📋 Board"])

# ---------- chat tab ----------
with tab_chat:
    up = st.file_uploader("Upload research file", type=["csv", "xlsx", "xls", "pdf"])
    if up is not None:
        key = f"{up.name}-{up.size}"
        if key not in st.session_state.done_files:
            st.session_state.done_files.add(key)
            say("user", f"Uploaded {up.name}")
            rows, dups, invalid, failed = [], 0, 0, False
            with st.spinner("Reading and sorting your file..."):
                try:
                    records = extract_records(file_to_text(up))
                    rows, dups, invalid = clean(records)
                except Exception as e:
                    failed = True
                    say("assistant", f"I couldn't read that file: {e}")
            if rows:
                st.session_state.pending = {"rows": rows}
                say(
                    "assistant",
                    f"I found **{len(rows)} agencies**: "
                    f"{sum(1 for r in rows if r['email'])} with emails, "
                    f"{sum(1 for r in rows if r['whatsapp'])} with WhatsApp numbers, "
                    f"{sum(1 for r in rows if r['instagram'])} with Instagram. "
                    f"Removed {dups} duplicates and {invalid} rows with no usable contact.\n\n"
                    "Check the table below. Should I replace the old data in your sheet? "
                    "(Your old data gets backed up first.)",
                )
            elif not failed:
                say("assistant", "I couldn't find any usable agencies in that file. Try another file?")
            st.rerun()

    for role, text in st.session_state.messages:
        with st.chat_message(role):
            st.markdown(text)

    if st.session_state.pending:
        st.dataframe(pd.DataFrame(st.session_state.pending["rows"]), use_container_width=True)
        c1, c2 = st.columns(2)
        c1.button("✅ Yes, write to sheet", on_click=approve, use_container_width=True)
        c2.button("❌ No, cancel", on_click=cancel, use_container_width=True)

    if st.session_state.ask_start:
        c1, c2 = st.columns(2)
        c1.button("🚀 Yes, start emails now", on_click=start_emails, use_container_width=True)
        c2.button("⏸ Not now", on_click=skip_start, use_container_width=True)

# ---------- board tab ----------
with tab_board:
    if st.button("🔄 Refresh"):
        refresh_board()
        st.rerun()

    autoclear_if_finished()
    b = st.session_state.get("board")

    if not b:
        st.warning(f"Couldn't load the sheet: {st.session_state.get('board_error', 'no data yet')}")
    else:
        e = b["email"]
        st.subheader("📧 Emails")
        if e["total"] == 0:
            st.caption("No emails in the queue.")
        else:
            processed = e["sent"] + e["errors"]
            st.progress(min(processed / e["total"], 1.0))
            state = "sending" if e["running"] else ("finished" if processed >= e["total"] else "not started")
            st.caption(f"{e['sent']} sent, {e['errors']} errors, {e['total']} total. Status: {state}")
            if not e["running"] and processed < e["total"]:
                if st.button("🚀 Start emails now"):
                    start_emails()
                    st.rerun()

        left, right = st.columns(2)

        with left:
            st.subheader("💬 WhatsApp")
            items = b["whatsapp"]
            st.caption(f"{sum(1 for x in items if x['done'])}/{len(items)} done")
            for i, x in enumerate(items):
                c1, c2, c3 = st.columns([5, 2, 2])
                c1.markdown(f":green[✅ {x['name']}]" if x["done"] else f"**{x['name']}**")
                url = "https://wa.me/" + normalize_number(x["value"]) + "?text=" + quote(wa_msg(x["name"]))
                c2.link_button("Open", url, disabled=x["done"])
                c3.button("Done ✓", key=f"wa-{x['row']}", on_click=mark,
                          args=("WhatsApp", x["row"], "whatsapp", i), disabled=x["done"])

        with right:
            st.subheader("📸 Instagram")
            items = b["instagram"]
            st.caption(f"{sum(1 for x in items if x['done'])}/{len(items)} done. Copy this message, then paste it in each DM:")
            st.code(IG_MSG, language=None)
            for i, x in enumerate(items):
                c1, c2, c3 = st.columns([5, 2, 2])
                c1.markdown(f":green[✅ {x['name']}]" if x["done"] else f"**{x['name']}**")
                c2.link_button("Open", "https://ig.me/m/" + x["value"], disabled=x["done"])
                c3.button("Done ✓", key=f"ig-{x['row']}", on_click=mark,
                          args=("Instagram", x["row"], "instagram", i), disabled=x["done"])

# ---------- typed replies ----------
msg = st.chat_input("Type yes / no, or upload a file in the Chat tab")
if msg:
    say("user", msg)
    t = msg.strip().lower()
    yes = t in ("yes", "y", "ok", "confirm", "approve", "do it", "start")
    no = t in ("no", "n", "cancel", "stop")
    if st.session_state.pending and yes:
        approve()
    elif st.session_state.pending and no:
        cancel()
    elif st.session_state.ask_start and yes:
        start_emails()
    elif st.session_state.ask_start and no:
        skip_start()
    else:
        say("assistant", "Upload a CSV, Excel or PDF file in the Chat tab and I'll sort it for you.")
    st.rerun()
