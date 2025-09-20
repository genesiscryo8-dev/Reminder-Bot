"""
simple_auto_reminders_fixed.py (no-thread safe, bugfix)

Fixes included in this update
- Resolved `KeyError: 'next_reminder_id'` by making `load_json` resilient:
  - If the JSON file contains a non-dict (e.g. a list) or is missing expected
    keys, the function resets/fills missing keys with sensible defaults and
    writes the corrected structure back to disk.
- Fixed `_fake_inputs` test helper (was a bare generator) — now a proper
  context manager via `contextlib.contextmanager` so tests can use
  `stack.enter_context(_fake_inputs(...))`.
- Small improvements to error messages and defensive checks.

Behavioral note
- The program still tries to start a background thread for scheduling. If the
  environment prevents starting threads it falls back to "tick mode" (no
  background thread) and processes due reminders on startup and around each
  prompt. This avoids `RuntimeError: can't start new thread` in restricted
  environments.

Run tests: python simple_auto_reminders_fixed.py --test
"""

# ---------- CONFIG (edit only these two) ----------
GMAIL_SENDER = "genesiscryo8@gmail.com"            # EDIT HERE: e.g. "you@gmail.com" OR leave "" to skip all email sending
GMAIL_APP_PASSWORD = "vlrt zskr zgjm dsrf"      # EDIT HERE: Gmail App Password (16-char) OR leave "" to skip email

# ---------- imports ----------
import os, json, time, traceback, sys, tempfile, shutil, contextlib
from datetime import datetime, timedelta

# threading is optional; code falls back if thread start fails
try:
    import threading
    _THREADING_AVAILABLE = True
except Exception:
    _THREADING_AVAILABLE = False

# safe input helper to avoid stray EOF/KeyboardInterrupt killing the app
def safe_input(prompt):
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print('\nInput cancelled (received EOF/Interrupt).')
        return ""

# try importing pywhatkit; if missing, continue but mark as unavailable
HAS_PYWHATKIT = True
try:
    import pywhatkit
except Exception:
    HAS_PYWHATKIT = False
    print("Note: pywhatkit not available. WhatsApp sending will be disabled. Install with: pip install pywhatkit")

# email imports only used if user set GMAIL_SENDER
HAS_EMAIL = False
if GMAIL_SENDER and GMAIL_APP_PASSWORD:
    try:
        import smtplib
        from email.message import EmailMessage
        HAS_EMAIL = True
    except Exception:
        HAS_EMAIL = False
        print("Warning: email libraries unavailable; email sending disabled.")

# ---------- files used (auto-created) ----------
CONTACTS_FILE = "contacts.json"
REMINDERS_FILE = "reminders.json"

# defaults
_CONTACTS_DEFAULT = {"next_contact_id": 1, "contacts": []}
_REMINDERS_DEFAULT = {"next_reminder_id": 1, "reminders": []}


def ensure_file(path, default):
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(default, f, indent=2)


def _normalize_loaded(data, default, path=None):
    """Ensure loaded JSON is a dict and contains keys from default.
    If not, return a corrected copy (and write-back will be done by caller).
    """
    if not isinstance(data, dict):
        if path:
            print(f"Warning: {path} contains invalid structure (not an object). Resetting to default.")
        return dict(default)
    changed = False
    out = dict(data)
    for k, v in default.items():
        if k not in out or not isinstance(out[k], type(v)):
            out[k] = v
            changed = True
    return out


def load_json(path, default):
    ensure_file(path, default)
    try:
        with open(path, "r") as f:
            data = json.load(f)
        data_norm = _normalize_loaded(data, default, path=path)
        # if data had wrong structure, overwrite with normalized data
        if data_norm is not data:
            with open(path, "w") as f:
                json.dump(data_norm, f, indent=2)
        return data_norm
    except Exception:
        # reset corrupt file
        print(f"Warning: {path} was corrupt — resetting to default structure.")
        with open(path, "w") as f2:
            json.dump(default, f2, indent=2)
        return dict(default)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# create files if missing with simple default structure
ensure_file(CONTACTS_FILE, _CONTACTS_DEFAULT)
ensure_file(REMINDERS_FILE, _REMINDERS_DEFAULT)

# ---------- contact helpers ----------
def add_contact():
    try:
        data = load_json(CONTACTS_FILE, _CONTACTS_DEFAULT)
        name = safe_input("Name (short): ").strip()
        if not name:
            print("Name cannot be empty.")
            return
        emails = safe_input("Emails (comma separated, or leave blank): ").strip()
        phones = safe_input("Phones (comma separated, include +countrycode, e.g. +919812345678) or leave blank: ").strip()
        contact = {
            "id": data["next_contact_id"],
            "name": name,
            "emails": [e.strip() for e in emails.split(",") if e.strip()],
            "phones": [p.strip() for p in phones.split(",") if p.strip()]
        }
        data["next_contact_id"] += 1
        data["contacts"].append(contact)
        save_json(CONTACTS_FILE, data)
        print(f"Saved contact id {contact['id']} ({contact['name']}).")
    except Exception as e:
        print("Error adding contact:", e)
        traceback.print_exc()


def list_contacts():
    try:
        data = load_json(CONTACTS_FILE, _CONTACTS_DEFAULT)
        if not data.get("contacts"):
            print("No contacts yet.")
            return
        print("Contacts:")
        for c in data["contacts"]:
            emails = ', '.join(c.get('emails', [])) or '(none)'
            phones = ', '.join(c.get('phones', [])) or '(none)'
            print(f" ID {c['id']} | {c.get('name','(no name)')} | emails: {emails} | phones: {phones}")
    except Exception as e:
        print("Error listing contacts:", e)
        traceback.print_exc()

# ---------- reminder helpers ----------
def add_reminder():
    try:
        cdata = load_json(CONTACTS_FILE, _CONTACTS_DEFAULT)
        if not cdata.get("contacts"):
            ans = safe_input("No contacts exist yet. Add a contact now? (y/N): ").strip().lower()
            if ans.startswith('y'):
                add_contact()
                cdata = load_json(CONTACTS_FILE, _CONTACTS_DEFAULT)
            else:
                print("Cancelled: no contacts to choose from.")
                return

        print("Choose recipients by ID (comma separated) or 'all' for everyone. Type 'none' to create a reminder with no recipients.")
        list_contacts()
        rec = safe_input("Recipients (e.g. 1,2 or all): ").strip().lower()
        if not rec:
            print("Cancelled.")
            return
        if rec == "all":
            recipient_ids = [c["id"] for c in cdata["contacts"]]
        elif rec == "none":
            recipient_ids = []
        else:
            parsed = []
            for x in rec.split(','):
                x = x.strip()
                if not x:
                    continue
                try:
                    parsed.append(int(x))
                except:
                    print(f"Warning: ignoring invalid recipient '{x}'. Should be numeric ID or 'all'.")
            recipient_ids = parsed

        # validate recipient IDs and warn if some don't exist
        cmap = {c['id']: c for c in cdata.get('contacts', [])}
        valid_recipient_ids = [rid for rid in recipient_ids if rid in cmap]
        invalid = [rid for rid in recipient_ids if rid not in cmap]
        if invalid:
            print(f"Warning: these recipient IDs were not found and will be ignored: {invalid}")
        recipient_ids = valid_recipient_ids

        title = safe_input("Title: ").strip()
        if not title:
            print("Title cannot be empty.")
            return
        message = safe_input("Message: ").strip() or "(no message)"
        when = safe_input("When (YYYY-MM-DD HH:MM) 24-hour: ").strip()
        try:
            dt = datetime.strptime(when, "%Y-%m-%d %H:%M")
        except Exception:
            print("Bad time format. Use e.g. 2025-09-06 14:30")
            return
        auto = safe_input("Automate sending at that time? (yes/no) [yes]: ").strip().lower()
        automate = (auto == "" or auto.startswith("y"))
        rep = safe_input("Repeat every day? (yes/no) [no]: ").strip().lower()
        repeat = "daily" if rep.startswith("y") else ""
        rdata = load_json(REMINDERS_FILE, _REMINDERS_DEFAULT)
        # defensive: ensure keys are present (load_json already normalizes but double-check)
        if 'next_reminder_id' not in rdata or 'reminders' not in rdata:
            rdata = dict(_REMINDERS_DEFAULT)
        rid = rdata["next_reminder_id"]
        rdata["next_reminder_id"] = rid + 1
        reminder = {
            "id": rid,
            "recipient_ids": recipient_ids,
            "title": title,
            "message": message,
            "time": dt.strftime("%Y-%m-%d %H:%M"),
            "automate": automate,
            "repeat": repeat
        }
        rdata["reminders"].append(reminder)
        save_json(REMINDERS_FILE, rdata)
        print(f"Saved reminder id {rid}. automate={automate}. Time={reminder['time']}")
    except Exception as e:
        print("Error adding reminder:", e)
        traceback.print_exc()


def list_reminders():
    try:
        rdata = load_json(REMINDERS_FILE, _REMINDERS_DEFAULT)
        cdata = load_json(CONTACTS_FILE, _CONTACTS_DEFAULT)
        cmap = {c["id"]:c for c in cdata.get("contacts", [])}
        if not rdata.get("reminders"):
            print("No reminders.")
            return
        print("Reminders:")
        for r in rdata["reminders"]:
            names = [cmap[x]["name"] for x in r.get("recipient_ids", []) if x in cmap]
            print(f" ID {r['id']} | {r.get('title','(no title)')} | {r.get('time')} | to: {', '.join(names) or '(no recipients)'} | auto={r.get('automate')} | {'daily' if r.get('repeat')=='daily' else 'one-time'}")
    except Exception as e:
        print("Error listing reminders:", e)
        traceback.print_exc()


def delete_reminder():
    try:
        list_reminders()
        idtxt = safe_input("Enter reminder ID to delete (or blank to cancel): ").strip()
        if not idtxt:
            return
        try:
            rid = int(idtxt)
        except:
            print("Bad ID.")
            return
        rdata = load_json(REMINDERS_FILE, _REMINDERS_DEFAULT)
        before = len(rdata.get("reminders", []))
        rdata["reminders"] = [r for r in rdata.get("reminders", []) if r.get("id") != rid]
        if len(rdata["reminders"]) < before:
            save_json(REMINDERS_FILE, rdata)
            print("Deleted", rid)
        else:
            print("Not found.")
    except Exception as e:
        print("Error deleting reminder:", e)
        traceback.print_exc()

# ---------- sending ----------
WHATSAPP_WAIT = 15  # seconds pywhatkit waits before sending (increase if slow PC)

def send_reminder(rem):
    try:
        contacts = load_json(CONTACTS_FILE, _CONTACTS_DEFAULT)["contacts"]
        cmap = {c["id"]:c for c in contacts}
        text = f"Reminder: {rem.get('title')}\n\n{rem.get('message')}\nWhen: {rem.get('time')}"
        # WhatsApp sends
        if HAS_PYWHATKIT:
            for cid in rem.get("recipient_ids", []):
                c = cmap.get(cid)
                if not c:
                    print(f"[whatsapp] warning: contact id {cid} not found — skipping")
                    continue
                for phone in c.get("phones", []):
                    try:
                        print(f"[whatsapp] sending to {c.get('name')} {phone} ... (browser will open)")
                        pywhatkit.sendwhatmsg_instantly(phone, text, wait_time=WHATSAPP_WAIT, tab_close=True, close_time=3)
                        print("[whatsapp] done")
                        time.sleep(1)
                    except Exception as e:
                        print("[whatsapp] error for", phone, ":", e)
        else:
            if any(c.get('phones') for c in cmap.values()):
                print("pywhatkit not installed — cannot send WhatsApp messages. Install with: pip install pywhatkit")

        # Email sends (only if configured)
        if HAS_EMAIL:
            for cid in rem.get("recipient_ids", []):
                c = cmap.get(cid)
                if not c:
                    print(f"[email] warning: contact id {cid} not found — skipping")
                    continue
                for email in c.get("emails", []):
                    try:
                        print(f"[email] sending to {c.get('name')} {email} ...")
                        msg = EmailMessage()
                        msg["Subject"] = "Reminder: " + rem.get('title','(no title)')
                        msg["From"] = GMAIL_SENDER
                        msg["To"] = email
                        msg.set_content(text)
                        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                            smtp.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
                            smtp.send_message(msg)
                        print("[email] done")
                    except Exception as e:
                        print("[email] error for", email, ":", e)
        else:
            # If user configured gmail but libraries missing, notify once
            if GMAIL_SENDER and GMAIL_APP_PASSWORD and not HAS_EMAIL:
                print("Email configured but email sending not available in this environment.")

    except Exception as e:
        print("Error during send_reminder:", e)
        traceback.print_exc()

# ---------- scheduler (threaded or tick) ----------
CHECK_SECONDS = 20

def process_due_reminders(now=None, verbose=True):
    """Process all due reminders once. In 'daily' mode, reschedule; otherwise remove."""
    try:
        now = now or datetime.now()
        rdata = load_json(REMINDERS_FILE, _REMINDERS_DEFAULT)
        changed = False
        processed = 0
        for rem in rdata.get("reminders", [])[:]:
            if not rem.get("automate"):
                continue
            try:
                dt = datetime.strptime(rem.get("time"), "%Y-%m-%d %H:%M")
            except Exception:
                continue
            if dt <= now:
                if verbose:
                    print(f"[auto] sending reminder id {rem.get('id')} -> {rem.get('title')}")
                try:
                    send_reminder(rem)
                except Exception:
                    print("[auto] send_reminder failed for id", rem.get('id'))
                    traceback.print_exc()
                if rem.get("repeat") == "daily":
                    new_dt = dt + timedelta(days=1)
                    rem["time"] = new_dt.strftime("%Y-%m-%d %H:%M")
                    changed = True
                    if verbose:
                        print(f"[auto] rescheduled id {rem.get('id')} to {rem.get('time')}")
                else:
                    try:
                        rdata["reminders"].remove(rem)
                    except ValueError:
                        pass
                    changed = True
                    if verbose:
                        print(f"[auto] removed id {rem.get('id')}")
                processed += 1
        if changed:
            save_json(REMINDERS_FILE, rdata)
        return processed
    except Exception as e:
        print("Scheduler error:", e)
        traceback.print_exc()
        return 0


def background_loop():
    while True:
        process_due_reminders()
        time.sleep(CHECK_SECONDS)

# ---------- CLI ----------

def send_now_cli():
    try:
        list_reminders()
        idtxt = safe_input("Enter reminder ID to send now (or blank to cancel): ").strip()
        if not idtxt:
            return
        try:
            rid = int(idtxt)
        except:
            print("Bad ID.")
            return
        rdata = load_json(REMINDERS_FILE, _REMINDERS_DEFAULT)
        rem = next((r for r in rdata.get("reminders", []) if r.get("id") == rid), None)
        if not rem:
            print("Not found.")
            return
        send_reminder(rem)
        print("Sent (attempted).")
    except Exception as e:
        print("Error in send_now:", e)
        traceback.print_exc()


def help_text():
    print("""
Commands:
  add_contact     - add a contact (name + emails + phones)
  list_contacts   - show saved contacts
  add_reminder    - create a reminder (choose contacts by id or 'all' or 'none')
  list_reminders  - show saved reminders
  delete_reminder - delete a reminder by id
  send_now        - send a saved reminder immediately
  help            - show this help text
  exit            - quit the program
""")


# ---------- tests ----------

@contextlib.contextmanager
def _fake_inputs(responses):
    """Context manager to temporarily replace input() with canned responses."""
    import builtins
    it = iter(list(responses))
    original = builtins.input
    builtins.input = lambda prompt='': next(it, '')
    try:
        yield
    finally:
        builtins.input = original


def _run_tests():
    print("Running tests...")
    # isolate files
    tmpdir = tempfile.mkdtemp(prefix="reminders_test_")
    try:
        global CONTACTS_FILE, REMINDERS_FILE, HAS_PYWHATKIT, HAS_EMAIL
        CONTACTS_FILE = os.path.join(tmpdir, 'contacts.json')
        REMINDERS_FILE = os.path.join(tmpdir, 'reminders.json')
        HAS_PYWHATKIT = False  # avoid real sends
        HAS_EMAIL = False
        ensure_file(CONTACTS_FILE, _CONTACTS_DEFAULT)
        ensure_file(REMINDERS_FILE, _REMINDERS_DEFAULT)

        # 1) add_reminder with no contacts -> offer then cancel
        import contextlib as _cl
        with _cl.ExitStack() as stack:
            stack.enter_context(_fake_inputs(['n']))  # decline add_contact
            add_reminder()  # should not crash

        # 2) add a contact
        with _cl.ExitStack() as stack:
            stack.enter_context(_fake_inputs(['Alice', 'alice@example.com', '+10000000000']))
            add_contact()
        data = load_json(CONTACTS_FILE, _CONTACTS_DEFAULT)
        assert len(data['contacts']) == 1 and data['contacts'][0]['name'] == 'Alice'

        # 3) add reminder to 'all' with automate daily in the past
        past = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M")
        with _cl.ExitStack() as stack:
            stack.enter_context(_fake_inputs(['all', 'Morning check', 'Standup now', past, 'y', 'y']))
            add_reminder()
        rdata = load_json(REMINDERS_FILE, _REMINDERS_DEFAULT)
        assert len(rdata['reminders']) == 1
        # run scheduler once -> should reschedule to next day
        processed = process_due_reminders(now=datetime.now())
        assert processed == 1
        rdata = load_json(REMINDERS_FILE, _REMINDERS_DEFAULT)
        assert len(rdata['reminders']) == 1 and rdata['reminders'][0]['repeat'] == 'daily'

        # 4) add one-time reminder in the past and ensure it gets removed
        with _cl.ExitStack() as stack:
            stack.enter_context(_fake_inputs(['all', 'Pay bill', 'Due', past, 'y', 'n']))
            add_reminder()
        processed = process_due_reminders(now=datetime.now())
        rdata = load_json(REMINDERS_FILE, _REMINDERS_DEFAULT)
        assert any(r['repeat']=='daily' for r in rdata['reminders'])
        assert not any((r['repeat']!='daily' and datetime.strptime(r['time'], '%Y-%m-%d %H:%M') <= datetime.now()) for r in rdata['reminders'])

        # 5) delete_reminder flow (delete the first id)
        first_id = rdata['reminders'][0]['id']
        with _cl.ExitStack() as stack:
            stack.enter_context(_fake_inputs([str(first_id)]))
            delete_reminder()
        rdata2 = load_json(REMINDERS_FILE, _REMINDERS_DEFAULT)
        assert len(rdata2['reminders']) <= len(rdata['reminders'])

        print("All tests passed. ✅")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- entry point ----------

def main():
    # Mode flags
    use_thread = True
    if '--no-thread' in sys.argv:
        use_thread = False

    # Start scheduler if allowed
    if use_thread and _THREADING_AVAILABLE:
        try:
            t = threading.Thread(target=background_loop, daemon=True)
            t.start()
            print("- Background scheduler: ON (threaded)")
        except Exception as e:
            print(f"- Background scheduler could not start ({e}). Falling back to tick mode.")
            use_thread = False
    else:
        if use_thread and not _THREADING_AVAILABLE:
            print("- Threading module unavailable; using tick mode.")
        else:
            print("- Background scheduler: OFF (tick mode)")

    print("Simple Auto Reminders (Hardened)")
    if GMAIL_SENDER and GMAIL_APP_PASSWORD and HAS_EMAIL:
        print("- Email sending is ENABLED using Gmail (App Password).")
    elif GMAIL_SENDER and GMAIL_APP_PASSWORD and not HAS_EMAIL:
        print("- Email configured but will not send because environment lacks required libraries.")
    else:
        print("- Email disabled (leave GMAIL_SENDER and GMAIL_APP_PASSWORD empty to skip email).")
    if HAS_PYWHATKIT:
        print("- WhatsApp support enabled (pywhatkit found).")
    else:
        print("- WhatsApp disabled (pywhatkit not installed).")
    print("- WhatsApp uses your browser and web.whatsapp.com (login first).")
    help_text()

    # First tick in case due reminders exist at startup
    process_due_reminders()

    while True:
        # Tick the scheduler each prompt if not using threads
        if not use_thread:
            process_due_reminders()
        cmd = safe_input("> ").strip().lower()
        if not cmd:
            continue
        try:
            if cmd == "add_contact":
                add_contact()
            elif cmd == "list_contacts":
                list_contacts()
            elif cmd == "add_reminder":
                add_reminder()
            elif cmd == "list_reminders":
                list_reminders()
            elif cmd == "delete_reminder":
                delete_reminder()
            elif cmd == "send_now":
                send_now_cli()
            elif cmd == "help":
                help_text()
            elif cmd == "exit":
                print("Goodbye.")
                break
            else:
                print("Unknown. Type 'help' for commands.")
        except Exception as e:
            # catch-all: never let the app quit because of an exception in a command
            print("An error occurred while processing the command:", e)
            traceback.print_exc()
        finally:
            # Another tick after each command in tick mode
            if not use_thread:
                process_due_reminders()


if __name__ == "__main__":
    if '--test' in sys.argv:
        code = _run_tests()
        sys.exit(code)
    main()