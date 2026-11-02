#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SUPER ACCOUNTING - Live Mobile Sync & API Server
Serves www/ mobile assets and handles real-time synchronization with accounting_data.db
"""

import os
import sys
import json
import sqlite3
import datetime
import urllib.parse
import urllib.request
import socket
import threading
import re
import difflib
from http.server import HTTPServer, SimpleHTTPRequestHandler

# Directory resolution
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WWW_DIR = os.path.join(BASE_DIR, "www")
PARENT_DIR = os.path.dirname(BASE_DIR)

# Locate accounting_data.db
DB_CANDIDATES = [
    os.path.join(PARENT_DIR, "accounting_data.db"),
    os.path.join(BASE_DIR, "accounting_data.db"),
    os.path.join(BASE_DIR, "assets", "accounting_data.db")
]

DB_PATH = None
for p in DB_CANDIDATES:
    if os.path.isfile(p):
        DB_PATH = p
        break

if not DB_PATH:
    DB_PATH = os.path.join(PARENT_DIR, "accounting_data.db")

print(f"[Mobile Sync Server] Using Database: {DB_PATH}")
print(f"[Mobile Sync Server] Serving Web Assets from: {WWW_DIR}")

def get_owner_email():
    """Retrieve registered Gmail from app_settings"""
    try:
        if os.path.isfile(DB_PATH):
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT value FROM app_settings WHERE key='gmail_connected_email'")
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                return str(row[0]).strip().lower()
    except Exception:
        pass
    return None

OWNER_EMAIL = get_owner_email()
print(f"[Mobile Sync Server] Registered Owner Email: {OWNER_EMAIL}")

def init_sync_logs_table(conn):
    """Ensure mobile_sync_logs table and indices exist"""
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mobile_sync_logs (
                log_id TEXT PRIMARY KEY,
                timestamp TEXT,
                gmail_id TEXT,
                device_name TEXT,
                customer_name TEXT,
                action_type TEXT,
                amount REAL DEFAULT 0.0,
                description TEXT,
                entry_mode TEXT,
                is_viewed INTEGER DEFAULT 0
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sync_logs_ts ON mobile_sync_logs(timestamp)")
        conn.commit()
    except Exception as e:
        print("[DB Init Logs Error]:", e)

def sync_db_to_initial_json():
    """Exports live accounting_data.db data into www/data/initial_data.json"""
    try:
        if not os.path.isfile(DB_PATH):
            return
        target_dir = os.path.join(WWW_DIR, "data")
        os.makedirs(target_dir, exist_ok=True)
        target_json = os.path.join(target_dir, "initial_data.json")

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        init_sync_logs_table(conn)
        cur = conn.cursor()

        cur.execute("SELECT id, name FROM companies WHERE id=7")
        comp_row = cur.fetchone()
        if not comp_row:
            cur.execute("SELECT id, name FROM companies LIMIT 1")
            comp_row = cur.fetchone()
        comp_dict = dict(comp_row) if comp_row else None

        cur.execute("""
            SELECT 
                c.id, c.company_id, c.name, c.mobile, c.address, c.opening_balance, c.date, c.due_date, c.email, c.memo,
                (
                    SELECT date FROM invoices 
                    WHERE customer_id = c.id AND tx_type = 'DEBIT' 
                    ORDER BY 
                        CASE 
                            WHEN substr(date, 5, 1) = '-' THEN date
                            WHEN substr(date, 3, 1) = '-' THEN substr(date, 7, 4) || '-' || substr(date, 4, 2) || '-' || substr(date, 1, 2)
                            WHEN substr(date, 3, 1) = '/' THEN substr(date, 7, 4) || '-' || substr(date, 4, 2) || '-' || substr(date, 1, 2)
                            ELSE date
                        END DESC, id DESC
                    LIMIT 1
                ) AS latest_debit_date
            FROM customers c 
            WHERE c.company_id = ?
        """, (comp_dict["id"],))
        customers = []
        for r in cur.fetchall():
            c_dict = dict(r)
            c_dict["display_date"] = c_dict.get("latest_debit_date") or c_dict.get("date") or ""
            customers.append(c_dict)

        cur.execute("SELECT id, customer_id, tx_type, amount, date, due_date, memo FROM invoices")
        invoices = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT key, value FROM app_settings")
        settings = {r["key"]: r["value"] for r in cur.fetchall()}

        cur.execute("SELECT log_id, timestamp, gmail_id, device_name, customer_name, action_type, amount, description, entry_mode, is_viewed FROM mobile_sync_logs ORDER BY timestamp DESC LIMIT 500")
        sync_logs = [dict(r) for r in cur.fetchall()]
        conn.close()

        payload = {
            "company": comp_dict,
            "customers": customers,
            "invoices": invoices,
            "sync_logs": sync_logs,
            "settings": settings
        }

        tmp = target_json + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        if os.path.exists(target_json):
            os.remove(target_json)
        os.rename(tmp, target_json)

        # Also write bundled initial_data.js for instant offline APK loading
        target_js = os.path.join(target_dir, "initial_data.js")
        with open(target_js, "w", encoding="utf-8") as f:
            f.write(f"window.__BUNDLED_INITIAL_DATA__ = {json.dumps(payload, ensure_ascii=False)};")
    except Exception as e:
        print("[Sync] initial_data.json export error:", e)


# =========================================================================
# LOCAL WI-FI IP DETECTION & CLOUD RELAY BRIDGE
# =========================================================================
def get_local_ip():
    """Detects primary Wi-Fi / Local Area Network IP of this machine"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_cloud_sync_topic(email=None):
    clean_email = re.sub(r'[^a-z0-9]', '_', (email or OWNER_EMAIL or "user").lower())
    return f"super_cloud_sync_{clean_email}"


def push_to_cloud_relay(db_path=DB_PATH, email=None):
    """Pushes fresh accounting database to ntfy Cloud Relay for 5G mobile apps"""
    try:
        sync_db_to_initial_json()
        target_json = os.path.join(WWW_DIR, "data", "initial_data.json")
        if not os.path.isfile(target_json):
            return
        with open(target_json, "r", encoding="utf-8") as f:
            raw_bytes = f.read().encode("utf-8")

        topic = get_cloud_sync_topic(email or OWNER_EMAIL)
        url = f"https://ntfy.sh/{topic}"
        req = urllib.request.Request(
            url,
            data=raw_bytes,
            headers={
                "Title": "SUPER_ACCOUNTING_DB_SYNC",
                "Filename": "super_accounting_data.json",
                "Tags": "cloud_sync,database"
            },
            method="PUT"
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            print(f"[Mobile Sync Server] Cloud Relay updated ({len(raw_bytes)} bytes, HTTP {resp.status})")
    except Exception as e:
        print("[Mobile Sync Server] Cloud Relay push note:", e)


def push_to_cloud_relay_async(db_path=DB_PATH, email=None):
    t = threading.Thread(target=push_to_cloud_relay, args=(db_path, email), daemon=True)
    t.start()

# =========================================================================
# HINDI DEVANAGARI TRANSLITERATION, PHONETICS & VOICE PARSING (REQUIREMENT 3 & 4)
# =========================================================================
DEVANAGARI_CONSONANTS = {
    'क': 'k', 'ख': 'kh', 'ग': 'g', 'घ': 'gh', 'ङ': 'ng',
    'च': 'ch', 'छ': 'chh', 'ज': 'j', 'झ': 'jh', 'ञ': 'ny',
    'ट': 't', 'ठ': 'th', 'ड': 'd', 'ढ': 'dh', 'ण': 'n',
    'त': 't', 'थ': 'th', 'द': 'd', 'ध': 'dh', 'न': 'n',
    'प': 'p', 'फ': 'ph', 'ब': 'b', 'भ': 'bh', 'म': 'm',
    'य': 'y', 'र': 'r', 'ल': 'l', 'व': 'v',
    'श': 'sh', 'ष': 'sh', 'स': 's', 'ह': 'h',
    'ड़': 'd', 'ढ़': 'dh', 'ज़': 'z', 'फ़': 'f', 'क़': 'q', 'ख़': 'kh', 'ग़': 'gh'
}
DEVANAGARI_VOWELS = {
    'अ': 'a', 'आ': 'aa', 'इ': 'i', 'ई': 'ee', 'उ': 'u', 'ऊ': 'oo',
    'ऋ': 'ri', 'ए': 'e', 'ऐ': 'ai', 'ओ': 'o', 'औ': 'au',
    'अं': 'an', 'अः': 'ah'
}
DEVANAGARI_MATRAS = {
    'ा': 'a', 'ि': 'i', 'ी': 'ee', 'ु': 'u', 'ू': 'u',
    'ृ': 'ri', 'े': 'e', 'ै': 'ai', 'ो': 'o', 'ौ': 'au',
    'ं': 'n', 'ँ': 'n', 'ः': 'h'
}
DEVANAGARI_DIGITS = {
    '०': '0', '१': '1', '२': '2', '३': '3', '४': '4',
    '५': '5', '६': '6', '७': '7', '८': '8', '९': '9'
}
HINDI_NUM_WORDS = {
    'एक': 1, 'दो': 2, 'तीन': 3, 'चार': 4, 'पांच': 5, 'पाँच': 5, 'छह': 6, 'सात': 7, 'आठ': 8, 'नौ': 9, 'दस': 10,
    'बीस': 20, 'तीस': 30, 'चालीस': 40, 'पचास': 50, 'साठ': 60, 'सत्तर': 70, 'अस्सी': 80, 'नब्बे': 90,
    'सौ': 100, 'हज़ार': 1000, 'हजार': 1000, 'लाख': 100000,
    'sau': 100, 'hazar': 1000, 'hajaar': 1000, 'pachas': 50, 'das': 10, 'bees': 20, 'tees': 30
}

def devanagari_to_roman(text):
    if not text:
        return ''
    s = ''
    for ch in text:
        s += DEVANAGARI_DIGITS.get(ch, ch)
    res = ''
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in DEVANAGARI_VOWELS:
            res += DEVANAGARI_VOWELS[ch]
        elif ch in DEVANAGARI_CONSONANTS:
            base = DEVANAGARI_CONSONANTS[ch]
            if i + 1 < len(s) and s[i+1] == '्':
                res += base
                i += 1
            elif i + 1 < len(s) and s[i+1] in DEVANAGARI_MATRAS:
                res += base + DEVANAGARI_MATRAS[s[i+1]]
                i += 1
            else:
                if i + 1 == len(s) or s[i+1] in ' \t\n,.-?!/':
                    res += base
                else:
                    res += base + 'a'
        elif ch in DEVANAGARI_MATRAS:
            res += DEVANAGARI_MATRAS[ch]
        else:
            res += ch
        i += 1
    return res

def phonetic_token(w):
    w = (w or '').lower().strip()
    w = w.replace('y', 'i').replace('ee', 'i').replace('oo', 'u').replace('u', 'a').replace('w', 'v')
    w = w.replace('ph', 'f').replace('sh', 's').replace('z', 'j').replace('l', 'r')
    return re.sub(r'(.)\1+', r'\1', w)

def normalize_date_str(d_str, default=""):
    if not d_str or not isinstance(d_str, str): return default
    d_str = d_str.strip()
    if not d_str: return default
    if re.match(r"^\d{2}-\d{2}-\d{4}$", d_str): return d_str
    if re.match(r"^\d{2}/\d{2}/\d{4}$", d_str): return d_str.replace("/", "-")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", d_str):
        parts = d_str.split("-")
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    m = re.match(r"^(\d{1,2})[-/. ](\d{1,2})[-/. ](\d{2,4})$", d_str)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100: year += 2000
        return f"{day:02d}-{month:02d}-{year:04d}"
    return default

def parse_voice_command_text(text):
    if not text or not text.strip():
        return None
    raw = text.strip()
    roman = devanagari_to_roman(raw)
    combined = (raw + ' ' + roman).lower()
    today_str = datetime.date.today().strftime('%d-%m-%Y')

    # 1. Determine tx_type
    credit_kws = ['जमा', 'credit', 'jama', 'mile', 'mila', 'aaya', 'diye', 'die', 'di', 'payment', 'chukta', 'vasool']
    debit_kws = ['उधार', 'debit', 'udhar', 'naam', 'maal diya', 'bheja', 'saman', 'bill', 'baaki', 'baki', 'khareeda']

    tx_type = 'DEBIT'
    for kw in credit_kws:
        if kw in combined:
            tx_type = 'CREDIT'
            break
    if tx_type != 'CREDIT':
        for kw in debit_kws:
            if kw in combined:
                tx_type = 'DEBIT'
                break

    # 2. Extract Date & Due Date (Requirement 1 & 2)
    has_due_kw = any(kw in combined for kw in ['due date', 'due', 'vayda', 'vada', 'ड्यू डेट', 'ड्यू', 'वायदा', 'वादा', 'देय', 'देगा'])
    date_matches = re.findall(r'\b(\d{1,2}[-/. ]\d{1,2}[-/. ]\d{2,4})\b', raw + ' ' + roman)
    
    clean_text = raw
    entry_date = today_str
    due_date = ""

    if len(date_matches) == 1:
        entry_date = normalize_date_str(date_matches[0], default=today_str)
        due_date = ""
        clean_text = clean_text.replace(date_matches[0], ' ')
    elif len(date_matches) >= 2:
        entry_date = normalize_date_str(date_matches[0], default=today_str)
        due_date = normalize_date_str(date_matches[1], default="")
        clean_text = clean_text.replace(date_matches[0], ' ').replace(date_matches[1], ' ')

    # 3. Extract Item / Description (Requirement 3)
    item_keywords = [
        ('सीलिंग फैन', 'Ceiling Fan (सीलिंग फैन)'),
        ('सीलिंगफैन', 'Ceiling Fan (सीलिंग फैन)'),
        ('पंखा', 'Fan (पंखा)'),
        ('फैन', 'Fan'),
        ('मोटर', 'Motor (मोटर)'),
        ('तार', 'Wire (तार)'),
        ('वायर', 'Wire'),
        ('पाइप', 'Pipe (पाइप)'),
        ('कूलर', 'Cooler (कूलर)'),
        ('पंप', 'Pump (पंप)'),
        ('स्विच', 'Switch'),
        ('सॉकेट', 'Socket'),
        ('बोर्ड', 'Board'),
        ('ceiling fan', 'Ceiling Fan'),
        ('motor', 'Motor'),
        ('wire', 'Wire'),
        ('pipe', 'Pipe'),
        ('fan', 'Fan')
    ]
    extracted_item = ""
    for kw, label in item_keywords:
        pattern = r'(?:\b|\s|^)' + re.escape(kw) + r'(?:\b|\s|$)'
        m_item = re.search(pattern, clean_text, re.IGNORECASE)
        if m_item:
            extracted_item = label
            clean_text = clean_text[:m_item.start()] + ' ' + clean_text[m_item.end():]
            break

    # 4. Extract Address / Village
    extracted_addr = ""
    if re.search(r'(?:जिलेवाड़ा|jilewada)', clean_text, re.IGNORECASE):
        extracted_addr = "Jirewada"
        clean_text = re.sub(r'(?:जिलेवाड़ा|jilewada)', ' ', clean_text, flags=re.IGNORECASE)
    elif re.search(r'(?:जिरेवाड़ा|jirewada)', clean_text, re.IGNORECASE):
        extracted_addr = "Jirewada"
        clean_text = re.sub(r'(?:जिरेवाड़ा|jirewada)', ' ', clean_text, flags=re.IGNORECASE)

    # 5. Determine amount
    amount = 0.0
    num_match = re.search(r'\b(\d+(?:\.\d+)?)\b', devanagari_to_roman(clean_text))
    if num_match:
        amount = float(num_match.group(1))
        clean_text = clean_text.replace(num_match.group(1), ' ')
    else:
        for hw, val in HINDI_NUM_WORDS.items():
            if hw in clean_text:
                amount = float(val)
                clean_text = clean_text.replace(hw, ' ')
                break

    # 6. Extract clean name words
    stop_words = {
        'रुपये', 'रुपया', 'rupaye', 'rupayee', 'rs', 'rupees', 'inr', 'ka', 'ki', 'ke', 'ko', 'se', 'me', 'mein', 'men',
        'jama', 'karo', 'likho', 'likh', 'udhar', 'credit', 'debit', 'diya', 'diye', 'die', 'di', 'mila', 'aaya', 'hai', 'tha',
        'जमा', 'करो', 'लिखो', 'उधार', 'का', 'की', 'के', 'को', 'से', 'में', 'दिया', 'दिए', 'मिला', 'आया', 'है', 'था', 'माल', 'बाकी'
    }
    for hw in HINDI_NUM_WORDS.keys():
        stop_words.add(hw)
        stop_words.add(devanagari_to_roman(hw).lower())

    clean_roman = devanagari_to_roman(clean_text)
    words = clean_roman.split()
    name_tokens = []
    for w in words:
        clean = re.sub(r'[^a-zA-Z]', '', w).lower()
        if clean and clean not in stop_words and not re.match(r'^\d+$', w):
            name_tokens.append(clean)

    extracted_name = ' '.join(name_tokens).strip()
    return {
        'raw_text': raw,
        'roman': roman,
        'tx_type': tx_type,
        'amount': amount,
        'extracted_name': extracted_name,
        'extracted_address': extracted_addr,
        'extracted_item': extracted_item,
        'date': entry_date,
        'due_date': due_date,
        'memo': extracted_item or f"Voice Command ({'जमा' if tx_type == 'CREDIT' else 'उधार'})"
    }

def find_best_matching_customer_db(conn, company_id, query_name):
    if not query_name:
        return None
    cur = conn.cursor()
    cur.execute("SELECT id, name, mobile, address, opening_balance FROM customers WHERE company_id = ?", (company_id,))
    rows = cur.fetchall()
    if not rows:
        return None

    custs = [
        {"id": r[0], "name": r[1] or "", "mobile": r[2] or "", "address": r[3] or "", "opening_balance": float(r[4] or 0.0)}
        for r in rows
    ]

    q_lower = query_name.lower().strip()
    q_words = [w for w in q_lower.split() if len(w) >= 2]
    q_phon = phonetic_token(q_lower)

    # Level 1: Exact match
    for c in custs:
        if c["name"].strip().lower() == q_lower:
            return c

    # Level 2: Substring match
    for c in custs:
        c_lower = c["name"].strip().lower()
        if len(c_lower) >= 2 and (q_lower in c_lower or (len(c_lower) >= 3 and c_lower in q_lower)):
            return c
        c_words = [w for w in c_lower.split() if len(w) >= 2]
        if q_words and all(qw in c_words for qw in q_words):
            return c

    # Level 3: Phonetic and Fuzzy match (difflib SequenceMatcher >= 0.65)
    best_cust = None
    best_score = 0.0
    for c in custs:
        c_phon = phonetic_token(c["name"])
        if c_phon and (c_phon == q_phon or c_phon in q_phon or q_phon in c_phon):
            return c
        ratio = difflib.SequenceMatcher(None, q_phon, c_phon).ratio()
        if ratio > best_score:
            best_score = ratio
            best_cust = c

    if best_score >= 0.65:
        return best_cust
    return None

class MobileSyncRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WWW_DIR, **kwargs)

    def end_headers(self):
        # Enable CORS
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            
            cust_cnt = 0
            inv_cnt = 0
            if os.path.isfile(DB_PATH):
                try:
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("SELECT count(*) FROM customers WHERE company_id=7")
                    cust_cnt = c.fetchone()[0]
                    c.execute("SELECT count(*) FROM invoices")
                    inv_cnt = c.fetchone()[0]
                    conn.close()
                except Exception:
                    pass

            resp = {
                "status": "online",
                "wifi_ip": get_local_ip(),
                "port": getattr(self.server, 'server_port', 8080),
                "owner_email": OWNER_EMAIL,
                "db_path": DB_PATH,
                "customers_count": cust_cnt,
                "invoices_count": inv_cnt,
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        if path == "/api/sync_logs":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                init_sync_logs_table(conn)
                cur = conn.cursor()
                cur.execute("SELECT log_id, timestamp, gmail_id, device_name, customer_name, action_type, amount, description, entry_mode, is_viewed FROM mobile_sync_logs ORDER BY timestamp DESC LIMIT 500")
                logs = [dict(r) for r in cur.fetchall()]
                conn.close()
                self.wfile.write(json.dumps({"success": True, "logs": logs}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        if path == "/api/data":
            gmail = (query.get("gmail", [""])[0] or "").strip().lower()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()

            is_owner = (gmail == OWNER_EMAIL.lower()) or (not gmail and OWNER_EMAIL)

            if is_owner:
                # Read live from accounting_data.db
                try:
                    conn = sqlite3.connect(DB_PATH)
                    conn.row_factory = sqlite3.Row
                    init_sync_logs_table(conn)
                    cur = conn.cursor()

                    cur.execute("SELECT id, name FROM companies")
                    companies = [dict(r) for r in cur.fetchall()]
                    if not companies:
                        companies = []

                    # Preferred active company: first company
                    active_comp = next((c for c in companies if c["id"] == 7), companies[0])

                    cur.execute("SELECT id, company_id, name, mobile, address, opening_balance, date, due_date, email, memo FROM customers WHERE company_id = ?", (active_comp["id"],))
                    customers = [dict(r) for r in cur.fetchall()]

                    cur.execute("SELECT id, customer_id, tx_type, amount, date, due_date, memo FROM invoices")
                    invoices = [dict(r) for r in cur.fetchall()]

                    cur.execute("SELECT key, value FROM app_settings")
                    settings = {r["key"]: r["value"] for r in cur.fetchall()}

                    cur.execute("SELECT log_id, timestamp, gmail_id, device_name, customer_name, action_type, amount, description, entry_mode, is_viewed FROM mobile_sync_logs ORDER BY timestamp DESC LIMIT 500")
                    sync_logs = [dict(r) for r in cur.fetchall()]
                    conn.close()

                    sync_db_to_initial_json()

                    resp_data = {
                        "success": True,
                        "is_owner": True,
                        "email": OWNER_EMAIL,
                        "companies": companies,
                        "active_company_id": active_comp["id"],
                        "company": active_comp,
                        "customers": customers,
                        "invoices": invoices,
                        "sync_logs": sync_logs,
                        "settings": settings
                    }
                    self.wfile.write(json.dumps(resp_data, ensure_ascii=False).encode("utf-8"))
                    return
                except Exception as e:
                    self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
                    return
            else:
                # STRICT ISOLATION FOR NON-OWNER GMAIL
                accounts_dir = os.path.join(WWW_DIR, "data", "accounts")
                os.makedirs(accounts_dir, exist_ok=True)
                safe_name = "".join(c if c.isalnum() else "_" for c in gmail) + ".json"
                acc_file = os.path.join(accounts_dir, safe_name)

                if os.path.isfile(acc_file):
                    try:
                        with open(acc_file, "r", encoding="utf-8") as f:
                            saved_acc = json.load(f)
                            self.wfile.write(json.dumps(saved_acc, ensure_ascii=False).encode("utf-8"))
                            return
                    except Exception:
                        pass

                # Return fresh blank account (0 companies, 0 customers, 0 invoices)
                blank_acc = {
                    "success": True,
                    "is_owner": False,
                    "email": gmail,
                    "companies": [],
                    "active_company_id": None,
                    "company": None,
                    "customers": [],
                    "invoices": [],
                    "settings": {
                        "gemini_api_key": "AQ.Ab8RN6I_10YMJ9RrPimgd_09x2tcbkpKyvfLoNTc1vNE2-7gyw",
                        "gemini_model": "gemini-3.8-flash"
                    }
                }
                self.wfile.write(json.dumps(blank_acc, ensure_ascii=False).encode("utf-8"))
                return

        # Serve static files from WWW_DIR
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # REQUIREMENT 3: WORKING VOICE PARSER & DIRECT TRANSACTION ENDPOINT
        if path == "/api/voice_command":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body)
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Invalid JSON"}).encode("utf-8"))
                return

            gmail = str(data.get("gmail", "")).strip().lower()
            text = str(data.get("text", "")).strip()
            company_id = int(data.get("company_id", 7))
            is_owner = (gmail == OWNER_EMAIL.lower()) or (not gmail)

            if not text:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "कमांड टेक्स्ट खाली है (Empty voice text)."}).encode("utf-8"))
                return

            parsed = parse_voice_command_text(text)
            if not parsed or parsed["amount"] <= 0:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": False,
                    "error": "वॉइस कमांड में राशि (Amount) नहीं मिली।",
                    "parsed": parsed
                }).encode("utf-8"))
                return

            tx_type = parsed["tx_type"]
            amount = parsed["amount"]
            today_str = parsed["date"]
            query_name = parsed["extracted_name"]

            if is_owner:
                try:
                    conn = sqlite3.connect(DB_PATH)
                    cur = conn.cursor()

                    matched = find_best_matching_customer_db(conn, company_id, query_name)
                    if not matched:
                        new_name = query_name or "नया ग्राहक"
                        cur.execute("""
                            INSERT INTO customers (company_id, name, mobile, address, opening_balance, date, due_date, email, memo)
                            VALUES (?, ?, '', '', 0, ?, '', '', 'Voice Command Auto Created')
                        """, (company_id, new_name, today_str))
                        cid = cur.lastrowid
                        cname = new_name
                        cmobile = ""
                        caddr = ""
                    else:
                        cid = matched["id"]
                        cname = matched["name"]
                        cmobile = matched.get("mobile", "")
                        caddr = matched.get("address", "")

                    memo = parsed.get("memo") or f"Voice Command ({'जमा' if tx_type == 'CREDIT' else 'उधार'})"
                    cur.execute("""
                        INSERT INTO invoices (customer_id, tx_type, amount, date, due_date, memo)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (cid, tx_type, amount, today_str, parsed.get("due_date", ""), memo))
                    inv_id = cur.lastrowid
                    if tx_type == "DEBIT" and today_str:
                        cur.execute("UPDATE customers SET date = ? WHERE id = ?", (today_str, cid))

                    # Record in mobile_sync_logs
                    init_sync_logs_table(conn)
                    v_lid = f"log_voice_{inv_id}_{int(datetime.datetime.now().timestamp()*1000)}"
                    v_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    v_dev = str(data.get("device_name", "")).strip() or "Mobile Device (Voice AI)"
                    v_act = "जमा (Credit)" if tx_type == "CREDIT" else "उधार (Debit)"
                    cur.execute("""
                        INSERT OR REPLACE INTO mobile_sync_logs
                        (log_id, timestamp, gmail_id, device_name, customer_name, action_type, amount, description, entry_mode, is_viewed)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """, (v_lid, v_ts, OWNER_EMAIL, v_dev, cname, v_act, amount, memo, "Voice AI 🎤"))
                    conn.commit()

                    # Calculate updated balance
                    cur.execute("""
                        SELECT c.opening_balance + COALESCE(SUM(CASE WHEN i.tx_type='CREDIT' THEN -i.amount ELSE i.amount END), 0)
                        FROM customers c
                        LEFT JOIN invoices i ON c.id = i.customer_id
                        WHERE c.id = ?
                    """, (cid,))
                    bal_row = cur.fetchone()
                    updated_bal = float(bal_row[0]) if bal_row and bal_row[0] is not None else 0.0

                    conn.close()
                    sync_db_to_initial_json()
                    push_to_cloud_relay_async(DB_PATH, OWNER_EMAIL)

                    hindi_type = "जमा (Credit)" if tx_type == "CREDIT" else "उधार (Debit)"
                    resp_data = {
                        "success": True,
                        "source": "server_sqlite",
                        "customer": {
                            "id": cid,
                            "name": cname,
                            "mobile": cmobile,
                            "address": caddr,
                            "net_balance": updated_bal
                        },
                        "invoice_id": inv_id,
                        "tx_type": tx_type,
                        "amount": amount,
                        "date": today_str,
                        "memo": memo,
                        "message": f"{cname} के खाते में ₹{amount:,.2f} {hindi_type} दर्ज हो गया!"
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps(resp_data, ensure_ascii=False).encode("utf-8"))
                    return

                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
                    return
            else:
                try:
                    accounts_dir = os.path.join(WWW_DIR, "data", "accounts")
                    os.makedirs(accounts_dir, exist_ok=True)
                    safe_name = "".join(c if c.isalnum() else "_" for c in gmail) + ".json"
                    acc_file = os.path.join(accounts_dir, safe_name)

                    acc_data = {"companies": [], "customers": [], "invoices": []}
                    if os.path.isfile(acc_file):
                        with open(acc_file, "r", encoding="utf-8") as f:
                            acc_data = json.load(f)

                    cust_list = acc_data.get("customers", [])
                    inv_list = acc_data.get("invoices", [])

                    matched_c = None
                    for c in cust_list:
                        if query_name in (c.get("name") or "").lower():
                            matched_c = c
                            break
                    if not matched_c:
                        new_cid = max([c.get("id", 0) for c in cust_list] + [0]) + 1
                        matched_c = {
                            "id": new_cid,
                            "company_id": company_id,
                            "name": query_name or "New Customer",
                            "mobile": "",
                            "address": "",
                            "opening_balance": 0.0,
                            "date": today_str
                        }
                        cust_list.append(matched_c)

                    new_invid = max([i.get("id", 0) for i in inv_list] + [0]) + 1
                    memo = f"Voice Command ({'जमा' if tx_type == 'CREDIT' else 'उधार'})"
                    inv_list.append({
                        "id": new_invid,
                        "customer_id": matched_c["id"],
                        "tx_type": tx_type,
                        "amount": amount,
                        "date": today_str,
                        "due_date": "",
                        "memo": memo
                    })

                    acc_data["customers"] = cust_list
                    acc_data["invoices"] = inv_list

                    with open(acc_file, "w", encoding="utf-8") as f:
                        json.dump(acc_data, f, ensure_ascii=False, indent=2)

                    hindi_type = "जमा (Credit)" if tx_type == "CREDIT" else "उधार (Debit)"
                    resp_data = {
                        "success": True,
                        "source": "isolated_account",
                        "customer": matched_c,
                        "invoice_id": new_invid,
                        "tx_type": tx_type,
                        "amount": amount,
                        "date": today_str,
                        "memo": memo,
                        "message": f"{matched_c['name']} के खाते में ₹{amount:,.2f} {hindi_type} दर्ज हो गया!"
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps(resp_data, ensure_ascii=False).encode("utf-8"))
                    return
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
                    return

        if path == "/api/save":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body)
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Invalid JSON"}).encode("utf-8"))
                return

            gmail = str(data.get("gmail", "")).strip().lower()
            is_owner = (gmail == OWNER_EMAIL.lower())

            if is_owner:
                # Live sync directly into accounting_data.db
                try:
                    conn = sqlite3.connect(DB_PATH)
                    init_sync_logs_table(conn)
                    cur = conn.cursor()

                    req_type = data.get("type", "FULL_SYNC")
                    req_data = data.get("data", {})
                    req_device = str(data.get("device_name", "")).strip() or "Mobile Device"

                    # 1. Save explicit sync log if passed from mobile app
                    explicit_log = data.get("log")
                    if explicit_log and isinstance(explicit_log, dict) and explicit_log.get("log_id"):
                        cur.execute("""
                            INSERT OR REPLACE INTO mobile_sync_logs
                            (log_id, timestamp, gmail_id, device_name, customer_name, action_type, amount, description, entry_mode, is_viewed)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                        """, (
                            explicit_log.get("log_id"),
                            explicit_log.get("timestamp", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                            explicit_log.get("gmail_id", OWNER_EMAIL),
                            explicit_log.get("device_name", req_device),
                            explicit_log.get("customer_name", "N/A"),
                            explicit_log.get("action_type", "Manual"),
                            float(explicit_log.get("amount", 0.0) or 0.0),
                            explicit_log.get("description", ""),
                            explicit_log.get("entry_mode", "Manual ✍️")
                        ))
                    elif isinstance(data.get("sync_logs"), list):
                        for s_log in data.get("sync_logs"):
                            if isinstance(s_log, dict) and s_log.get("log_id"):
                                cur.execute("""
                                    INSERT OR IGNORE INTO mobile_sync_logs
                                    (log_id, timestamp, gmail_id, device_name, customer_name, action_type, amount, description, entry_mode, is_viewed)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                                """, (
                                    s_log.get("log_id"),
                                    s_log.get("timestamp", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                                    s_log.get("gmail_id", OWNER_EMAIL),
                                    s_log.get("device_name", req_device),
                                    s_log.get("customer_name", "N/A"),
                                    s_log.get("action_type", "Manual"),
                                    float(s_log.get("amount", 0.0) or 0.0),
                                    s_log.get("description", ""),
                                    s_log.get("entry_mode", "Manual ✍️")
                                ))

                    if req_type == "TRANSACTION":
                        # Add new transaction
                        tx = req_data
                        cur.execute("""
                            INSERT INTO invoices (customer_id, tx_type, amount, date, due_date, memo)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (tx.get("customer_id"), tx.get("tx_type", "DEBIT"), tx.get("amount", 0.0),
                              tx.get("date", ""), tx.get("due_date", ""), tx.get("memo", "")))
                        # Requirement 4: Update customer list date if Debit entry
                        if tx.get("tx_type") == "DEBIT" and tx.get("date"):
                            cur.execute("""
                                UPDATE customers SET date = ? WHERE id = ?
                            """, (tx.get("date"), tx.get("customer_id")))
                        conn.commit()

                        # Fallback log creation if not passed
                        if not explicit_log:
                            cur.execute("SELECT name FROM customers WHERE id = ?", (tx.get("customer_id"),))
                            crow = cur.fetchone()
                            cname = crow[0] if crow else "Unknown"
                            act = "जमा (Credit)" if tx.get("tx_type") == "CREDIT" else "उधार (Debit)"
                            lid = f"log_tx_{int(datetime.datetime.now().timestamp()*1000)}"
                            cur.execute("""
                                INSERT OR IGNORE INTO mobile_sync_logs
                                (log_id, timestamp, gmail_id, device_name, customer_name, action_type, amount, description, entry_mode, is_viewed)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                            """, (lid, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), OWNER_EMAIL, req_device,
                                  cname, act, float(tx.get("amount", 0.0) or 0.0), tx.get("memo", ""), "Manual ✍️"))
                            conn.commit()

                    elif req_type == "CUSTOMER_EDIT":
                        c = req_data
                        cur.execute("""
                            UPDATE customers
                            SET name = ?, mobile = ?, address = ?, opening_balance = ?
                            WHERE id = ?
                        """, (c.get("name"), c.get("mobile", ""), c.get("address", ""),
                              c.get("opening_balance", 0.0), c.get("id")))
                        conn.commit()

                        if not explicit_log:
                            lid = f"log_cedit_{int(datetime.datetime.now().timestamp()*1000)}"
                            cur.execute("""
                                INSERT OR IGNORE INTO mobile_sync_logs
                                (log_id, timestamp, gmail_id, device_name, customer_name, action_type, amount, description, entry_mode, is_viewed)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                            """, (lid, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), OWNER_EMAIL, req_device,
                                  c.get("name", "N/A"), "Edit", float(c.get("opening_balance", 0.0) or 0.0), "Customer Profile Updated", "Manual ✍️"))
                            conn.commit()

                    elif req_type == "CUSTOMER_ADD":
                        c = req_data
                        cur.execute("""
                            INSERT INTO customers (company_id, name, mobile, address, opening_balance, date, due_date, email, memo)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (c.get("company_id", 7), c.get("name"), c.get("mobile", ""), c.get("address", ""),
                              c.get("opening_balance", 0.0), c.get("date", ""), c.get("due_date", ""), "", ""))
                        new_cust_id = cur.lastrowid
                        conn.commit()

                        if not explicit_log:
                            lid = f"log_cadd_{int(datetime.datetime.now().timestamp()*1000)}"
                            cur.execute("""
                                INSERT OR IGNORE INTO mobile_sync_logs
                                (log_id, timestamp, gmail_id, device_name, customer_name, action_type, amount, description, entry_mode, is_viewed)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                            """, (lid, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), OWNER_EMAIL, req_device,
                                  c.get("name", "N/A"), "New Customer", float(c.get("opening_balance", 0.0) or 0.0), "New Customer Added", "Manual ✍️"))
                            conn.commit()

                    elif req_type == "TRANSACTION_EDIT":
                        t = req_data
                        cur.execute("""
                            UPDATE invoices
                            SET amount = ?, date = ?, due_date = ?, memo = ?
                            WHERE id = ?
                        """, (t.get("amount"), t.get("date"), t.get("due_date", ""), t.get("memo", ""), t.get("id")))
                        conn.commit()

                    elif req_type == "TRANSACTION_DELETE":
                        tid = req_data.get("id")
                        cur.execute("DELETE FROM invoices WHERE id = ?", (tid,))
                        conn.commit()

                    elif req_type == "CUSTOMER_DELETE":
                        cid = req_data.get("id")
                        cur.execute("DELETE FROM invoices WHERE customer_id = ?", (cid,))
                        cur.execute("DELETE FROM customers WHERE id = ?", (cid,))
                        conn.commit()

                    elif req_type == "COMPANY_ADD":
                        cname = req_data.get("name")
                        if cname:
                            cur.execute("INSERT INTO companies (name, created_at) VALUES (?, ?)",
                                        (cname, datetime.datetime.now().strftime("%d-%m-%Y")))
                            conn.commit()

                    elif req_type == "COMPANY_DELETE":
                        cid = req_data.get("id")
                        if cid and cid != 7: # prevent accidental deletion of main firm 7
                            cur.execute("DELETE FROM invoices WHERE customer_id IN (SELECT id FROM customers WHERE company_id = ?)", (cid,))
                            cur.execute("DELETE FROM customers WHERE company_id = ?", (cid,))
                            cur.execute("DELETE FROM companies WHERE id = ?", (cid,))
                            conn.commit()

                    elif req_type == "SETTINGS_UPDATE":
                        s = req_data
                        if "gemini_api_key" in s:
                            cur.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('gemini_api_key', ?)", (str(s["gemini_api_key"]).strip(),))
                        if "gemini_model" in s:
                            cur.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('gemini_model', ?)", (str(s["gemini_model"]).strip(),))
                        conn.commit()

                    elif req_type == "FULL_SYNC":
                        # Full sync from app
                        pass

                    conn.close()
                    sync_db_to_initial_json()
                    push_to_cloud_relay_async(DB_PATH, OWNER_EMAIL)

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True, "message": "Saved to live accounting_data.db"}).encode("utf-8"))
                    return
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
                    return
            else:
                # Save non-owner data in isolated file
                try:
                    accounts_dir = os.path.join(WWW_DIR, "data", "accounts")
                    os.makedirs(accounts_dir, exist_ok=True)
                    safe_name = "".join(c if c.isalnum() else "_" for c in gmail) + ".json"
                    acc_file = os.path.join(accounts_dir, safe_name)

                    with open(acc_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True, "message": "Saved to isolated account storage"}).encode("utf-8"))
                    return
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
                    return

        self.send_response(404)
        self.end_headers()

def run_server(port=8080):
    sync_db_to_initial_json()
    local_ip = get_local_ip()
    server_address = ("0.0.0.0", port)
    try:
        httpd = HTTPServer(server_address, MobileSyncRequestHandler)
    except OSError:
        port = 8081
        server_address = ("0.0.0.0", port)
        httpd = HTTPServer(server_address, MobileSyncRequestHandler)

    print("=" * 68)
    print("      SUPER ACCOUNTING - LIVE MOBILE SYNC SERVER ACTIVE")
    print("=" * 68)
    print(f"  Local PC Web App URL : http://localhost:{port}/index.html")
    print(f"  Wi-Fi / LAN Sync URL : http://{local_ip}:{port}/index.html")
    print(f"  Direct Sync API      : http://{local_ip}:{port}/api/save")
    print(f"  Live SQLite Database : {DB_PATH}")
    print(f"  Owner Email          : {OWNER_EMAIL}")
    print(f"  Cloud Sync Topic     : {get_cloud_sync_topic(OWNER_EMAIL)}")
    print("=" * 68)
    print("  TIP: Mobile APK me 'Sync Settings' (☁ icon) me yeh Wi-Fi IP enter karein:")
    print(f"       http://{local_ip}:{port}")
    print("=" * 68)
    print("Press Ctrl+C to stop the server.\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        httpd.server_close()

if __name__ == "__main__":
    p = 8080
    if len(sys.argv) > 1:
        try:
            p = int(sys.argv[1])
        except ValueError:
            pass
    run_server(p)
