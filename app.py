"""
Daily Pigmy Collection App  (Streamlit + SQLite)
-------------------------------------------------
Roles
  * Agent : opens accounts, collects daily deposits, sees own customers
  * Admin : everything an agent can + dashboard, agents, payouts, reports

Interest
  Simple interest on EVERY deposit from the day it was paid until maturity:
      interest = amount * rate% * (days_to_maturity / 365)

Passbook PDF
  * Uses a Unicode font so the rupee sign prints (falls back to "Rs." if none is found).
    Streamlit Cloud: add packages.txt with  fonts-dejavu-core  (or drop NotoSans-Regular.ttf /
    NotoSans-Bold.ttf into a ./fonts folder next to app.py).
  * Logo, signature image, address and signatory title are set on the admin "Settings" page.

Default login (change immediately!):  admin / admin123
Override with env/secret ADMIN_PASSWORD before first run.
"""
import base64
import hashlib
import os
import re
import secrets
import sqlite3
import zipfile
from datetime import date, datetime, timedelta
from io import BytesIO
from xml.sax.saxutils import escape

import pandas as pd
import streamlit as st
from PIL import Image as PILImage
from reportlab.graphics.shapes import Circle, Drawing, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, HRFlowable, KeepTogether, PageBreak,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)
from reportlab.platypus import Image as RLImage

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PIGMY_DB", "pigmy.db")
COMPANY_NAME = os.environ.get("COMPANY_NAME", "Pigmy Collection Co.")   # default; editable in Settings page
PREMATURE_INTEREST_FACTOR = 0.0   # 0 = principal only on premature closure, 0.5 = half interest
CUR = "₹"

st.set_page_config(page_title="Pigmy Collection", page_icon="💰", layout="wide")


# ----------------------------------------------------------------- database
def conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA foreign_keys=ON")
    return c


def run(sql, params=()):
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def q(sql, params=()):
    with conn() as c:
        return pd.read_sql_query(sql, c, params=params)


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${h}"


def check_pw(pw, stored):
    salt, _ = stored.split("$")
    return secrets.compare_digest(hash_pw(pw, salt), stored)


def init_db():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, username TEXT UNIQUE, name TEXT,
                password TEXT, role TEXT, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS customers(
                id INTEGER PRIMARY KEY, name TEXT, phone TEXT, address TEXT,
                agent_id INTEGER, created_on TEXT);
            CREATE TABLE IF NOT EXISTS accounts(
                id INTEGER PRIMARY KEY, customer_id INTEGER, daily_amount REAL,
                start_date TEXT, tenure_days INTEGER, rate REAL,
                status TEXT DEFAULT 'ACTIVE');
            CREATE TABLE IF NOT EXISTS collections(
                id INTEGER PRIMARY KEY, account_id INTEGER, coll_date TEXT,
                amount REAL, agent_id INTEGER, created_at TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS payouts(
                id INTEGER PRIMARY KEY, account_id INTEGER, paid_on TEXT,
                principal REAL, interest REAL, total REAL, kind TEXT, paid_by INTEGER);
            """
        )
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            pw = os.environ.get("ADMIN_PASSWORD")
            try:
                pw = pw or st.secrets["ADMIN_PASSWORD"]
            except Exception:
                pass
            c.execute(
                "INSERT INTO users(username,name,password,role) VALUES(?,?,?,?)",
                ("admin", "Administrator", hash_pw(pw or "admin123"), "admin"),
            )


# ------------------------------------------------------------- business logic
def inr(x, dec=2):
    """Indian digit grouping: 1234567.5 -> 12,34,567.50"""
    whole, _, frac = f"{abs(x):.{dec}f}".partition(".")
    if len(whole) > 3:
        head, tail, parts = whole[:-3], whole[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if x < 0 else "") + whole + (f".{frac}" if dec else "")


def money(x):
    return f"{CUR}{inr(x)}"


def accounts_df(agent_id=None):
    """One row per account with principal, interest, payable, missed days etc."""
    sql = """SELECT a.id AS acc_id, c.name AS customer, c.phone, u.name AS agent,
                    c.agent_id, a.daily_amount, a.start_date, a.tenure_days,
                    a.rate, a.status
             FROM accounts a JOIN customers c ON c.id=a.customer_id
             JOIN users u ON u.id=c.agent_id"""
    params = ()
    if agent_id:
        sql += " WHERE c.agent_id=?"
        params = (agent_id,)
    df = q(sql, params)
    if df.empty:
        return df
    df["start_date"] = pd.to_datetime(df["start_date"])
    df["maturity_date"] = df["start_date"] + pd.to_timedelta(df["tenure_days"], unit="D")
    col = q("SELECT account_id, coll_date, amount FROM collections")
    df["principal"], df["interest"], df["days_paid"] = 0.0, 0.0, 0
    if not col.empty:
        col["coll_date"] = pd.to_datetime(col["coll_date"])
        m = col.merge(df[["acc_id", "maturity_date", "rate"]], left_on="account_id", right_on="acc_id")
        days = (m["maturity_date"] - m["coll_date"]).dt.days.clip(lower=0)
        m["interest"] = m["amount"] * m["rate"] / 100 * days / 365
        g = m.groupby("acc_id").agg(principal=("amount", "sum"), interest=("interest", "sum"),
                                    days_paid=("coll_date", "nunique"))
        df = df.drop(columns=["principal", "interest", "days_paid"]).merge(g, on="acc_id", how="left")
        df = df.fillna({"principal": 0.0, "interest": 0.0, "days_paid": 0})
    df["interest"] = df["interest"].round(2)
    df["payable"] = df["principal"] + df["interest"]
    today = pd.Timestamp(date.today())
    df["expected_days"] = ((today - df["start_date"]).dt.days + 1).clip(lower=0).clip(upper=df["tenure_days"])
    df["missed_days"] = (df["expected_days"] - df["days_paid"]).clip(lower=0)
    df["matured"] = today >= df["maturity_date"]
    return df


def label(r):
    return f"#{r.acc_id} · {r.customer} · {CUR}{r.daily_amount:g}/day"


def add_collection(acc_id, amount, coll_date, agent_id):
    run("INSERT INTO collections(account_id,coll_date,amount,agent_id,created_at) VALUES(?,?,?,?,?)",
        (int(acc_id), str(coll_date), float(amount), int(agent_id), datetime.now().isoformat(timespec="seconds")))


# ------------------------------------------------ settings, fonts & passbook PDF
SETTING_DEFAULTS = {"company_name": COMPANY_NAME, "company_address": "", "company_phone": "",
                    "signatory": "Authorised Signatory", "logo_b64": "", "sign_b64": ""}


def get_settings():
    s = dict(SETTING_DEFAULTS)
    s.update({r.key: r.value or "" for r in q("SELECT key, value FROM settings").itertuples()})
    return s


def set_setting(key, value):
    run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


def prep_image(upload):
    """Uploaded logo/signature -> small PNG, base64 text (stored in the DB)."""
    im = PILImage.open(upload)
    im.thumbnail((600, 600))
    buf = BytesIO()
    im.convert("RGBA").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


FONT_PAIRS = [  # (regular, bold) - first pair that exists AND contains the rupee glyph wins
    (f"{HERE}/fonts/NotoSans-Regular.ttf", f"{HERE}/fonts/NotoSans-Bold.ttf"),
    (f"{HERE}/fonts/DejaVuSans.ttf", f"{HERE}/fonts/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
]


@st.cache_resource
def pdf_fonts():
    """Returns (regular_font, bold_font, rupee_symbol)."""
    for i, (reg, bold) in enumerate(FONT_PAIRS):
        if os.path.exists(reg) and os.path.exists(bold):
            try:
                f = TTFont(f"PB{i}", reg)
                if 0x20B9 not in f.face.charToGlyph:
                    continue
                pdfmetrics.registerFont(f)
                pdfmetrics.registerFont(TTFont(f"PB{i}-Bold", bold))
                pdfmetrics.registerFontFamily(f"PB{i}", normal=f"PB{i}", bold=f"PB{i}-Bold")
                return f"PB{i}", f"PB{i}-Bold", "₹"
            except Exception:
                continue
    return "Helvetica", "Helvetica-Bold", "Rs."


def _img(b64, max_w, max_h):
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        w, h = ImageReader(BytesIO(raw)).getSize()
        k = min(max_w / w, max_h / h)
        return RLImage(BytesIO(raw), width=w * k, height=h * k, mask="auto")
    except Exception:
        return None


class AcctMark(Flowable):
    """Zero-size marker so the footer knows where each passbook starts (page numbers restart)."""
    def __init__(self, label):
        super().__init__()
        self.label = label

    def wrap(self, aw, ah):
        return 0, 0

    def draw(self):
        pass


class PassbookDoc(BaseDocTemplate):
    font = "Helvetica"
    first_page, label = 1, ""

    def __init__(self, buf, **kw):
        super().__init__(buf, **kw)
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height,
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates([PageTemplate(id="p", frames=[frame], onPageEnd=self._footer)])

    def afterFlowable(self, fl):
        if isinstance(fl, AcctMark):
            self.first_page, self.label = self.page, fl.label

    def _footer(self, canvas, doc):
        canvas.saveState()
        canvas.setFont(self.font, 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 10 * mm, f"{self.label}  ·  Printed {datetime.now():%d-%b-%Y %H:%M}")
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Page {doc.page - self.first_page + 1}")
        canvas.restoreState()


def passbook_story(a, addr, tx, po, S):
    F, FB, R, cfg = S["font"], S["bold"], S["rupee"], S["cfg"]
    rs = lambda x: f"{R}{'' if R == '₹' else ' '}{inr(x)}"
    ink, accent, zebra = colors.HexColor("#1f2937"), colors.HexColor("#0f766e"), colors.HexColor("#f3f4f6")
    ss = getSampleStyleSheet()

    def P(**kw):
        base = dict(fontName=F, textColor=ink, fontSize=9, leading=11)
        base.update(kw)
        return ParagraphStyle("x", parent=ss["Normal"], **base)

    def grid(data, widths, header=True):
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
        style = [("FONTNAME", (0, 0), (-1, -1), F), ("FONTSIZE", (0, 0), (-1, -1), 9),
                 ("TEXTCOLOR", (0, 0), (-1, -1), ink), ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                 ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if header:
            style += [("BACKGROUND", (0, 0), (-1, 0), accent), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                      ("FONTNAME", (0, 0), (-1, 0), FB), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, zebra])]
        t.setStyle(TableStyle(style))
        return t

    # ---- header: logo + company block
    txt = [Paragraph(escape(cfg["company_name"]), P(fontName=FB, fontSize=18, leading=21, textColor=accent))]
    if cfg["company_address"].strip():
        txt.append(Paragraph(escape(cfg["company_address"]).replace("\n", "<br/>"),
                             P(fontSize=8.5, textColor=colors.grey)))
    if cfg["company_phone"].strip():
        txt.append(Paragraph("Phone: " + escape(cfg["company_phone"]), P(fontSize=8.5, textColor=colors.grey)))
    logo = _img(cfg["logo_b64"], 32 * mm, 20 * mm)
    head = Table([[logo, txt]], colWidths=[36 * mm, 144 * mm]) if logo else Table([[txt]], colWidths=[180 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    h2 = P(fontName=FB, fontSize=11, leading=14, spaceBefore=10, spaceAfter=4)
    story = [head, HRFlowable(width="100%", color=accent, thickness=1.2, spaceBefore=5, spaceAfter=4),
             Paragraph("PIGMY DEPOSIT PASSBOOK", P(fontName=FB, fontSize=10, alignment=1, textColor=colors.grey)),
             Spacer(1, 8)]

    # ---- account details
    info = [["Account No.", f"#{a.acc_id}", "Status", a.status],
            ["Customer", Paragraph(escape(a.customer), P()), "Phone", a.phone or "-"],
            ["Address", Paragraph(escape(addr), P()), "Agent", a.agent],
            ["Daily deposit", rs(a.daily_amount), "Interest rate", f"{a.rate:g}% p.a."],
            ["Start date", a.start_date.strftime("%d-%b-%Y"), "Maturity date", a.maturity_date.strftime("%d-%b-%Y")],
            ["Tenure", f"{a.tenure_days} days", "Days paid", f"{int(a.days_paid)} / {a.tenure_days}"]]
    t = grid(info, [28 * mm, 62 * mm, 28 * mm, 62 * mm], header=False)
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (0, -1), FB), ("FONTNAME", (2, 0), (2, -1), FB),
                           ("BACKGROUND", (0, 0), (0, -1), zebra), ("BACKGROUND", (2, 0), (2, -1), zebra),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [t, Paragraph("Summary", h2)]

    st_ = grid([["Total deposited", "Interest accrued", "Total payable"],
                [rs(a.principal), rs(a.interest), rs(a.payable)]], [60 * mm] * 3)
    st_.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("FONTNAME", (0, 1), (-1, 1), FB),
                             ("FONTSIZE", (0, 1), (-1, 1), 11)]))
    story += [st_, Paragraph("Transactions", h2)]

    # ---- transactions
    rows = [["#", "Date", f"Deposit ({R})", f"Balance ({R})"]]
    for i, r in enumerate(tx.itertuples(), 1):
        rows.append([i, pd.to_datetime(r.coll_date).strftime("%d-%b-%Y"), inr(r.amount), inr(r.balance)])
    if len(rows) == 1:
        rows.append(["-", "No deposits yet", "-", "-"])
    tt = grid(rows, [15 * mm, 55 * mm, 55 * mm, 55 * mm])
    tt.setStyle(TableStyle([("ALIGN", (2, 0), (-1, -1), "RIGHT"), ("ALIGN", (0, 0), (0, -1), "CENTER")]))
    story.append(tt)

    if not po.empty:
        p = po.iloc[0]
        story += [Paragraph("Payout", h2),
                  grid([["Paid on", "Type", "Principal", "Interest", "Total paid"],
                        [p.paid_on, p.kind, rs(p.principal), rs(p.interest), rs(p.total)]],
                       [30 * mm, 32 * mm, 40 * mm, 38 * mm, 40 * mm])]

    # ---- signature & stamp block
    d = Drawing(26 * mm, 26 * mm)
    d.add(Circle(13 * mm, 13 * mm, 12.5 * mm, strokeColor=colors.grey, strokeWidth=0.8,
                 strokeDashArray=[3, 2], fillColor=None))
    d.add(String(13 * mm, 14 * mm, "STAMP", textAnchor="middle", fontName=F, fontSize=8, fillColor=colors.lightgrey))
    d.hAlign = "CENTER"
    sign = _img(cfg["sign_b64"], 46 * mm, 20 * mm)
    lab = P(fontSize=8, alignment=1, textColor=colors.grey)
    blk = Table([["", "", d, "", sign or ""],
                 [Paragraph("Customer signature", lab), "", Paragraph("Company seal", lab), "",
                  Paragraph(f"{escape(cfg['signatory'])}<br/>for {escape(cfg['company_name'])}", lab)]],
                colWidths=[58 * mm, 6 * mm, 52 * mm, 6 * mm, 58 * mm], rowHeights=[28 * mm, None])
    blk.setStyle(TableStyle([("VALIGN", (0, 0), (-1, 0), "BOTTOM"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                             ("LINEABOVE", (0, 1), (0, 1), 0.6, ink), ("LINEABOVE", (4, 1), (4, 1), 0.6, ink),
                             ("TOPPADDING", (0, 1), (-1, 1), 3)]))
    story += [KeepTogether([Spacer(1, 14), blk, Spacer(1, 8),
                            Paragraph("Interest is simple interest on each deposit from its deposit date until "
                                      "maturity. Figures are system generated as of the print date.",
                                      P(fontSize=7.5, textColor=colors.grey))])]
    return story


def build_passbooks_pdf(acc_ids, df=None):
    """One PDF containing a passbook for every account id (each starts on a new page)."""
    acc_ids = [int(i) for i in acc_ids]
    df = accounts_df() if df is None else df
    cfg, (F, FB, R) = get_settings(), pdf_fonts()
    S = {"font": F, "bold": FB, "rupee": R, "cfg": cfg}
    addr = q("SELECT a.id AS acc_id, c.address FROM accounts a JOIN customers c ON c.id=a.customer_id"
             ).set_index("acc_id")["address"]
    col = q("SELECT account_id, coll_date, amount FROM collections ORDER BY coll_date, id")
    pay = q("SELECT account_id, paid_on, principal, interest, total, kind FROM payouts")
    story = []
    for n, aid in enumerate(acc_ids):
        a = df[df.acc_id == aid].iloc[0]
        tx = col[col.account_id == aid].copy()
        tx["balance"] = tx["amount"].cumsum()
        if n:
            story.append(PageBreak())
        story.append(AcctMark(f"Passbook #{aid} · {a.customer}"))
        story += passbook_story(a, addr.get(aid) or "-", tx, pay[pay.account_id == aid], S)
    buf = BytesIO()
    doc = PassbookDoc(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm,
                      bottomMargin=18 * mm, title="Pigmy passbook", author=cfg["company_name"])
    doc.font = F
    doc.build(story)
    return buf.getvalue()


def make_passbook(acc_id):
    return build_passbooks_pdf([acc_id])


# --------------------------------------------------------------------- login
def login_screen():
    st.title("💰 Pigmy Collection")
    with st.form("login"):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.form_submit_button("Login", type="primary"):
            row = q("SELECT * FROM users WHERE username=? AND active=1", (u.strip(),))
            if not row.empty and check_pw(p, row.iloc[0]["password"]):
                st.session_state.user = row.iloc[0][["id", "name", "role"]].to_dict()
                st.rerun()
            st.error("Invalid credentials")


# --------------------------------------------------------------------- pages
def page_dashboard():
    st.header("📊 Company Dashboard")
    df = accounts_df()
    today = str(date.today())
    tot = q("SELECT COALESCE(SUM(amount),0) s FROM collections").iloc[0, 0]
    tday = q("SELECT COALESCE(SUM(amount),0) s FROM collections WHERE coll_date=?", (today,)).iloc[0, 0]
    paid = q("SELECT COALESCE(SUM(total),0) s FROM payouts").iloc[0, 0]
    active = df[df.status == "ACTIVE"] if not df.empty else df
    liability = active["payable"].sum() if not active.empty else 0
    interest_liab = active["interest"].sum() if not active.empty else 0

    c = st.columns(4)
    c[0].metric("Customers", q("SELECT COUNT(*) n FROM customers").iloc[0, 0])
    c[1].metric("Active accounts", len(active))
    c[2].metric("Today's collection", money(tday))
    c[3].metric("Total collected", money(tot))
    c = st.columns(4)
    c[0].metric("Payouts made", money(paid))
    c[1].metric("Current liability (P+I)", money(liability))
    c[2].metric("Accrued interest liability", money(interest_liab))
    c[3].metric("Agents", q("SELECT COUNT(*) n FROM users WHERE role='agent' AND active=1").iloc[0, 0])

    since = str(date.today() - timedelta(days=29))
    left, right = st.columns(2)
    with left:
        st.subheader("Daily collections (30 days)")
        d = q("SELECT coll_date, SUM(amount) amount FROM collections WHERE coll_date>=? GROUP BY coll_date", (since,))
        if d.empty:
            st.info("No collections yet.")
        else:
            st.bar_chart(d.set_index("coll_date"))
    with right:
        st.subheader("Agent-wise collections")
        a = q("""SELECT u.name agent, SUM(c.amount) amount FROM collections c
                 JOIN users u ON u.id=c.agent_id GROUP BY u.name""")
        if a.empty:
            st.info("No collections yet.")
        else:
            st.bar_chart(a.set_index("agent"))

    if not active.empty:
        st.subheader("Maturing in next 30 days / already matured")
        soon = active[active.maturity_date <= pd.Timestamp(date.today() + timedelta(days=30))]
        st.dataframe(soon[["acc_id", "customer", "agent", "principal", "interest", "payable", "maturity_date"]],
                     hide_index=True, use_container_width=True)
        st.subheader("Defaulters (missed days)")
        miss = active[active.missed_days > 0].sort_values("missed_days", ascending=False)
        st.dataframe(miss[["acc_id", "customer", "phone", "agent", "missed_days"]].head(20),
                     hide_index=True, use_container_width=True)


def page_collect(user):
    st.header("🧾 Daily Collection")
    is_admin = user["role"] == "admin"
    df = accounts_df(None if is_admin else user["id"])
    if df.empty:
        st.info("No accounts yet. Open one under 'Customers & Accounts'.")
        return
    open_df = df[(df.status == "ACTIVE") & (~df.matured)]
    tab1, tab2 = st.tabs(["Route sheet (bulk)", "Single entry"])

    with tab1:
        d = st.date_input("Collection date", date.today(), key="route_date",
                          max_value=date.today())
        paid = q("SELECT account_id, SUM(amount) amt FROM collections WHERE coll_date=? GROUP BY account_id", (str(d),))
        route = open_df[["acc_id", "customer", "daily_amount"]].copy()
        route["paid_that_day"] = route["acc_id"].map(paid.set_index("account_id")["amt"]).fillna(0.0)
        route.insert(0, "collect", False)
        route["amount"] = route["daily_amount"]
        ed = st.data_editor(route, hide_index=True, use_container_width=True, key=f"route_{d}",
                            disabled=["acc_id", "customer", "daily_amount", "paid_that_day"])
        sel = ed[ed["collect"] & (ed["amount"] > 0)]
        st.caption(f"{len(sel)} selected · total {money(sel['amount'].sum())}")
        if st.button("Save collections", type="primary", disabled=sel.empty):
            for r in sel.itertuples():
                start = open_df.loc[open_df.acc_id == r.acc_id, "start_date"].iloc[0].date()
                if d < start:
                    st.warning(f"Account #{r.acc_id} starts on {start}; skipped.")
                    continue
                add_collection(r.acc_id, r.amount, d, user["id"])
            st.success("Saved.")
            st.rerun()

    with tab2:
        if open_df.empty:
            st.info("No active accounts.")
            return
        rows = {label(r): r for r in open_df.itertuples()}
        r = rows[st.selectbox("Account", list(rows))]
        c = st.columns(4)
        c[0].metric("Deposited", money(r.principal))
        c[1].metric("Interest so far", money(r.interest))
        c[2].metric("Days paid", f"{int(r.days_paid)}/{r.tenure_days}")
        c[3].metric("Missed days", int(r.missed_days))
        amt = st.number_input("Amount", min_value=0.0, value=float(r.daily_amount), step=float(r.daily_amount))
        cd = st.date_input("Date", date.today(), max_value=date.today(), key="single_date")
        st.download_button("📘 Passbook (PDF)", make_passbook(r.acc_id), f"passbook_{r.acc_id}.pdf",
                           "application/pdf", key="pb_single")
        if st.button("Collect", type="primary"):
            if amt <= 0:
                st.error("Enter an amount.")
            elif cd < r.start_date.date():
                st.error("Date is before account start date.")
            else:
                add_collection(r.acc_id, amt, cd, user["id"])
                st.success(f"Collected {money(amt)} from {r.customer} on {cd}.")
                st.code(f"PIGMY RECEIPT\nAccount : #{r.acc_id}\nCustomer: {r.customer}\n"
                        f"Date    : {cd}\nAmount  : {money(amt)}\nBalance : {money(r.principal + amt)}", language=None)

    hist = q("""SELECT c.id, c.coll_date, c.account_id, cu.name customer, c.amount
                FROM collections c JOIN accounts a ON a.id=c.account_id
                JOIN customers cu ON cu.id=a.customer_id
                WHERE c.coll_date=? """ + ("" if is_admin else "AND c.agent_id=?") + " ORDER BY c.id DESC",
             (str(date.today()),) if is_admin else (str(date.today()), user["id"]))
    st.subheader("Today's entries")
    st.dataframe(hist, hide_index=True, use_container_width=True)


def page_customers(user):
    st.header("👥 Customers & Accounts")
    is_admin = user["role"] == "admin"
    agents = q("SELECT id, name FROM users WHERE role='agent' AND active=1")

    with st.expander("➕ Open new account", expanded=False):
        cust = q("SELECT id, name, phone FROM customers" + ("" if is_admin else " WHERE agent_id=?"),
                 () if is_admin else (user["id"],))
        opts = ["➕ New customer"] + [f"#{r.id} {r.name} ({r.phone})" for r in cust.itertuples()]
        choice = st.selectbox("Customer", opts)
        with st.form("open_acc", clear_on_submit=True):
            if choice == opts[0]:
                name = st.text_input("Full name")
                phone = st.text_input("Phone")
                addr = st.text_area("Address")
            if is_admin:
                if agents.empty:
                    st.warning("Create an agent first.")
                ag = st.selectbox("Assigned agent", agents["name"].tolist()) if not agents.empty else None
            c = st.columns(4)
            daily = c[0].number_input("Daily amount", min_value=1.0, value=100.0, step=10.0)
            tenure = c[1].number_input("Tenure (days)", min_value=30, value=365, step=5)
            rate = c[2].number_input("Interest % p.a.", min_value=0.0, value=6.0, step=0.25)
            start = c[3].date_input("Start date", date.today())
            if st.form_submit_button("Create account", type="primary"):
                agent_id = user["id"]
                if is_admin and ag:
                    agent_id = int(agents.loc[agents.name == ag, "id"].iloc[0])
                if choice == opts[0]:
                    if not name.strip():
                        st.error("Name required")
                        st.stop()
                    cid = run("INSERT INTO customers(name,phone,address,agent_id,created_on) VALUES(?,?,?,?,?)",
                              (name.strip(), phone.strip(), addr.strip(), agent_id, str(date.today())))
                else:
                    cid = int(choice.split()[0][1:])
                aid = run("INSERT INTO accounts(customer_id,daily_amount,start_date,tenure_days,rate) VALUES(?,?,?,?,?)",
                          (cid, daily, str(start), int(tenure), rate))
                st.success(f"Account #{aid} opened. Matures on {start + timedelta(days=int(tenure))}.")

    df = accounts_df(None if is_admin else user["id"])
    if df.empty:
        st.info("No accounts yet.")
        return
    f = st.text_input("🔍 Search name / phone / account no.")
    if f:
        mask = (df.customer.str.contains(f, case=False) | df.phone.fillna("").str.contains(f)
                | (df.acc_id.astype(str) == f))
        df = df[mask]
    show = df[["acc_id", "customer", "phone", "agent", "daily_amount", "rate", "start_date", "maturity_date",
               "principal", "interest", "payable", "missed_days", "status"]].copy()
    for k in ("start_date", "maturity_date"):
        show[k] = show[k].dt.date
    st.dataframe(show, hide_index=True, use_container_width=True)

    st.subheader("Account statement")
    rows = {label(r): r.acc_id for r in df.itertuples()}
    if rows:
        aid = rows[st.selectbox("Account", list(rows))]
        st.download_button("📘 Download passbook (PDF)", make_passbook(aid), f"passbook_{aid}.pdf",
                           "application/pdf", type="primary")
        st.dataframe(q("SELECT coll_date AS date, amount FROM collections WHERE account_id=? ORDER BY coll_date DESC",
                       (int(aid),)), hide_index=True, use_container_width=True)


def page_payouts(user):
    st.header("🏦 Maturity & Payouts")
    df = accounts_df()
    if df.empty:
        st.info("No accounts.")
        return
    active = df[df.status == "ACTIVE"]
    st.subheader("Matured accounts ready for payout")
    ready = active[active.matured]
    st.dataframe(ready[["acc_id", "customer", "phone", "agent", "principal", "interest", "payable", "maturity_date"]],
                 hide_index=True, use_container_width=True)

    st.subheader("Process payout")
    if active.empty:
        st.info("No active accounts.")
    else:
        rows = {label(r): r for r in active.itertuples()}
        r = rows[st.selectbox("Account", list(rows), key="pay_acc")]
        premature = not r.matured
        if premature:
            st.warning(f"Not matured yet (matures {r.maturity_date.date()}). This will be a PREMATURE closure "
                       f"({PREMATURE_INTEREST_FACTOR:.0%} of interest paid).")
        interest = r.interest * (PREMATURE_INTEREST_FACTOR if premature else 1)
        total = r.principal + interest
        c = st.columns(3)
        c[0].metric("Principal", money(r.principal))
        c[1].metric("Interest", money(interest))
        c[2].metric("Total payable", money(total))
        if st.button("✅ Pay & close account", type="primary"):
            run("INSERT INTO payouts(account_id,paid_on,principal,interest,total,kind,paid_by) VALUES(?,?,?,?,?,?,?)",
                (int(r.acc_id), str(date.today()), r.principal, interest, total,
                 "PREMATURE" if premature else "MATURITY", user["id"]))
            run("UPDATE accounts SET status='CLOSED' WHERE id=?", (int(r.acc_id),))
            st.success(f"Paid {money(total)} to {r.customer}. Account closed.")
            st.rerun()

    st.subheader("Payout history")
    st.dataframe(q("""SELECT p.paid_on, p.account_id, cu.name customer, p.principal, p.interest, p.total, p.kind
                      FROM payouts p JOIN accounts a ON a.id=p.account_id
                      JOIN customers cu ON cu.id=a.customer_id ORDER BY p.id DESC"""),
                 hide_index=True, use_container_width=True)


def page_agents():
    st.header("🧑‍💼 Agents")
    with st.form("new_agent", clear_on_submit=True):
        c = st.columns(3)
        name, uname, pw = c[0].text_input("Name"), c[1].text_input("Username"), c[2].text_input("Password", type="password")
        if st.form_submit_button("Create agent", type="primary"):
            if not (name and uname and pw):
                st.error("All fields required")
            else:
                try:
                    run("INSERT INTO users(username,name,password,role) VALUES(?,?,?,'agent')",
                        (uname.strip(), name.strip(), hash_pw(pw)))
                    st.success("Agent created.")
                except sqlite3.IntegrityError:
                    st.error("Username already exists.")
    ag = q("SELECT id, username, name, active FROM users WHERE role='agent'")
    st.dataframe(ag, hide_index=True, use_container_width=True)
    if not ag.empty:
        c = st.columns(3)
        pick = c[0].selectbox("Agent", ag["username"])
        newpw = c[1].text_input("New password", type="password")
        if c[2].button("Reset password") and newpw:
            run("UPDATE users SET password=? WHERE username=?", (hash_pw(newpw), pick))
            st.success("Password updated.")
        if c[2].button("Toggle active"):
            run("UPDATE users SET active=1-active WHERE username=?", (pick,))
            st.rerun()


def page_reports():
    st.header("📁 Reports")
    c = st.columns(2)
    d1 = c[0].date_input("From", date.today() - timedelta(days=30))
    d2 = c[1].date_input("To", date.today())
    col = q("""SELECT c.coll_date, c.account_id, cu.name customer, u.name agent, c.amount
               FROM collections c JOIN accounts a ON a.id=c.account_id
               JOIN customers cu ON cu.id=a.customer_id JOIN users u ON u.id=c.agent_id
               WHERE c.coll_date BETWEEN ? AND ? ORDER BY c.coll_date""", (str(d1), str(d2)))
    st.subheader("Collections")
    st.dataframe(col, hide_index=True, use_container_width=True)
    st.download_button("⬇️ Download collections CSV", col.to_csv(index=False), "collections.csv")
    acc = accounts_df()
    if not acc.empty:
        st.subheader("Account summary")
        st.dataframe(acc.drop(columns=["agent_id"]), hide_index=True, use_container_width=True)
        st.download_button("⬇️ Download accounts CSV", acc.to_csv(index=False), "accounts.csv")


def page_bulk(user):
    st.header("🖨️ Bulk passbooks")
    is_admin = user["role"] == "admin"
    df = accounts_df(None if is_admin else user["id"])
    if df.empty:
        st.info("No accounts yet.")
        return
    c = st.columns(3)
    agent_id = None if is_admin else user["id"]
    if is_admin:
        ags = {f"{n}": i for i, n in df[["agent_id", "agent"]].drop_duplicates().itertuples(index=False)}
        pick = c[0].selectbox("Agent", ["All agents"] + sorted(ags))
        agent_id = None if pick == "All agents" else ags[pick]
    status = c[1].selectbox("Accounts", ["Active", "Closed", "All"])
    order = c[2].selectbox("Order by", ["Customer name", "Account no."])
    if agent_id:
        df = df[df.agent_id == agent_id]
    if status != "All":
        df = df[df.status == status.upper()]
    df = df.sort_values("customer" if order == "Customer name" else "acc_id")
    st.caption(f"{len(df)} passbook(s) · {df.agent.nunique()} agent(s)")
    st.dataframe(df[["acc_id", "customer", "agent", "principal", "interest", "status"]],
                 hide_index=True, use_container_width=True)
    split = is_admin and agent_id is None and st.checkbox("One PDF per agent (download as ZIP)")
    if st.button("Generate passbooks", type="primary", disabled=df.empty):
        with st.spinner(f"Building {len(df)} passbook(s)…"):
            full = accounts_df()
            if split:
                zbuf = BytesIO()
                with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
                    for aid_, g in df.groupby("agent_id"):
                        nm = re.sub(r"\W+", "_", g.agent.iloc[0])
                        z.writestr(f"passbooks_{nm}_{aid_}.pdf", build_passbooks_pdf(g.acc_id, full))
                st.session_state.bulk = (zbuf.getvalue(), f"passbooks_by_agent_{date.today()}.zip", "application/zip")
            else:
                st.session_state.bulk = (build_passbooks_pdf(df.acc_id, full),
                                         f"passbooks_{date.today()}.pdf", "application/pdf")
    if "bulk" in st.session_state:
        data, name, mime = st.session_state.bulk
        st.download_button(f"⬇️ Download {name}", data, name, mime)


def page_settings():
    st.header("⚙️ Company settings (printed on passbooks)")
    s = get_settings()
    with st.form("settings"):
        name = st.text_input("Company name", s["company_name"])
        addr = st.text_area("Address", s["company_address"])
        c = st.columns(2)
        phone = c[0].text_input("Phone", s["company_phone"])
        sig = c[1].text_input("Signatory title", s["signatory"])
        c = st.columns(2)
        logo = c[0].file_uploader("Logo (PNG/JPG)", type=["png", "jpg", "jpeg"])
        sign = c[1].file_uploader("Signature image (PNG, transparent background works best)",
                                  type=["png", "jpg", "jpeg"])
        rm_logo, rm_sign = c[0].checkbox("Remove logo"), c[1].checkbox("Remove signature")
        if st.form_submit_button("Save", type="primary"):
            for k, v in {"company_name": name.strip() or COMPANY_NAME, "company_address": addr.strip(),
                         "company_phone": phone.strip(), "signatory": sig.strip() or "Authorised Signatory"}.items():
                set_setting(k, v)
            try:
                for key, up, rm in (("logo_b64", logo, rm_logo), ("sign_b64", sign, rm_sign)):
                    if up:
                        set_setting(key, prep_image(up))
                    elif rm:
                        set_setting(key, "")
                st.success("Saved.")
            except Exception as e:
                st.error(f"Could not read the image: {e}")
    s = get_settings()
    c = st.columns(2)
    if s["logo_b64"]:
        c[0].image(base64.b64decode(s["logo_b64"]), caption="Current logo", width=140)
    if s["sign_b64"]:
        c[1].image(base64.b64decode(s["sign_b64"]), caption="Current signature", width=180)
    st.divider()
    accs = accounts_df()
    if not accs.empty:
        st.download_button("👁️ Preview sample passbook", make_passbook(accs.acc_id.iloc[0]),
                           "passbook_preview.pdf", "application/pdf")


# ---------------------------------------------------------------------- main
def main():
    init_db()
    if "user" not in st.session_state:
        login_screen()
        return
    user = st.session_state.user
    cfg = get_settings()
    if cfg["logo_b64"]:
        st.sidebar.image(base64.b64decode(cfg["logo_b64"]), width=90)
    st.sidebar.title(cfg["company_name"])
    st.sidebar.caption(f"{user['name']} · {user['role']}")
    pages = {"Collect": lambda: page_collect(user), "Customers & Accounts": lambda: page_customers(user),
             "Bulk passbooks": lambda: page_bulk(user)}
    if user["role"] == "admin":
        pages = {"Dashboard": page_dashboard, **pages, "Payouts": lambda: page_payouts(user),
                 "Agents": page_agents, "Reports": page_reports, "Settings": page_settings}
    choice = st.sidebar.radio("Menu", list(pages))
    if st.sidebar.button("Logout"):
        del st.session_state.user
        st.rerun()
    pages[choice]()


main()
