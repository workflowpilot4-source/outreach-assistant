import json
import re

import pandas as pd
import requests
import streamlit as st
from google import genai

st.set_page_config(page_title="Outreach Assistant", page_icon="✈️")

MODEL = st.secrets.get("GEMINI_MODEL", "gemini-2.5-flash")


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


# ---------- helpers ----------
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
                for table in page.extract_tables():
                    for row in table:
                        parts.append(" | ".join((c or "").replace("\n", " ") for c in row))
                if not page.extract_tables():
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
    'Use "" when a value is missing or says NOT FOUND. Never invent or guess any value. '
    "Text:\n"
)


def extract_records(text):
    client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
    records = []
    for c in chunk_text(text):
        resp = client.models.generate_content(
            model=MODEL,
            contents=PROMPT + c,
            config={"response_mime_type": "application/json"},
        )
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


def clean(records):
    seen_e, seen_n = set(), set()
    rows, dups, invalid = [], 0, 0
    for r in records:
        biz = str(r.get("business", "")).strip()
        email = str(r.get("email", "")).strip().lower()
        num = normalize_number(r.get("whatsapp", ""))
        ig = str(r.get("instagram", "")).strip()
        if ig.upper() == "NOT FOUND":
            ig = ""
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
            email = ""
        if not email and not num:
            invalid += 1
            continue
        if (email and email in seen_e) or (num and num in seen_n):
            dups += 1
            continue
        if email:
            seen_e.add(email)
        if num:
            seen_n.add(num)
        rows.append({"business": biz or "there", "email": email, "whatsapp": num, "instagram": ig})
    return rows, dups, invalid


def say(role, text):
    st.session_state.messages.append((role, text))


def approve():
    p = st.session_state.pending
    if not p:
        return
    payload = {
        "token": st.secrets["SHEET_TOKEN"],
        "raw": [[r["business"], r["email"], r["whatsapp"], r["instagram"]] for r in p["rows"]],
        "leads": [r["email"] for r in p["rows"] if r["email"]],
        "whatsapp": [[r["business"], r["whatsapp"]] for r in p["rows"] if r["whatsapp"]],
    }
    try:
        resp = requests.post(st.secrets["SHEET_URL"], json=payload, timeout=90)
        res = resp.json()
    except Exception as e:
        say("assistant", f"Something went wrong talking to the sheet: {e}")
        return
    if res.get("ok"):
        say(
            "assistant",
            f"Done. ✅ Old data was backed up and replaced.\n\n"
            f"- Leads: {res['leads']} emails\n- WhatsApp: {res['whatsapp']} numbers\n"
            f"- Import: {res['import_rows']} rows\n\nOpen your sheet and start sending.",
        )
        st.session_state.pending = None
    else:
        say("assistant", f"The sheet said: {res.get('error')}")


def cancel():
    st.session_state.pending = None
    say("assistant", "Okay, cancelled. Nothing was changed. Upload another file whenever you're ready.")


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
    st.session_state.done_files = set()

st.title("✈️ Outreach Assistant")

# ---------- file upload ----------
up = st.file_uploader("Upload research file", type=["csv", "xlsx", "xls", "pdf"])
if up is not None:
    key = f"{up.name}-{up.size}"
    if key not in st.session_state.done_files:
        st.session_state.done_files.add(key)
        say("user", f"Uploaded {up.name}")
        with st.spinner("Reading and sorting your file..."):
            try:
                text = file_to_text(up)
                records = extract_records(text)
                rows, dups, invalid = clean(records)
            except Exception as e:
                rows, dups, invalid = [], 0, 0
                say("assistant", f"I couldn't read that file: {e}")
        if rows:
            n_email = sum(1 for r in rows if r["email"])
            n_wa = sum(1 for r in rows if r["whatsapp"])
            st.session_state.pending = {"rows": rows}
            say(
                "assistant",
                f"I found **{len(rows)} agencies**: {n_email} with emails, {n_wa} with WhatsApp numbers. "
                f"Removed {dups} duplicates and {invalid} rows with no usable contact.\n\n"
                "Check the table below. Should I replace the old data in your sheet? "
                "(Your old data gets backed up first.)",
            )
        elif not any("couldn't read" in m[1] for m in st.session_state.messages[-1:]):
            say("assistant", "I couldn't find any usable agencies in that file. Try another file?")
        st.rerun()

# ---------- chat display ----------
for role, text in st.session_state.messages:
    with st.chat_message(role):
        st.markdown(text)

if st.session_state.pending:
    st.dataframe(pd.DataFrame(st.session_state.pending["rows"]), use_container_width=True)
    c1, c2 = st.columns(2)
    c1.button("✅ Yes, write to sheet", on_click=approve, use_container_width=True)
    c2.button("❌ No, cancel", on_click=cancel, use_container_width=True)

# ---------- typed replies ----------
msg = st.chat_input("Type yes / no, or upload a file above")
if msg:
    say("user", msg)
    t = msg.strip().lower()
    if st.session_state.pending and t in ("yes", "y", "ok", "confirm", "approve", "do it"):
        approve()
    elif st.session_state.pending and t in ("no", "n", "cancel", "stop"):
        cancel()
    else:
        say("assistant", "Upload a CSV, Excel or PDF file above and I'll sort it for you.")
    st.rerun()
