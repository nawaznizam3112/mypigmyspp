"""
Daily Pigmy Collection App  (Streamlit + SQLite)
-------------------------------------------------
Roles
  * Agent : opens accounts, collects daily deposits, sees own customers
  * Admin : everything an agent can + dashboard, agents, payouts, reports

Interest
  Simple interest on EVERY deposit from the day it was paid until maturity:
      interest = amount * rate% * (days_to_maturity / 365)

Default login (change immediately!):  admin / admin123
Override with env/secret ADMIN_PASSWORD before first run.
"""
import hashlib
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

DB_PATH = os.environ.get("PIGMY_DB", "pigmy.db")
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
def money(x):
    return f"{CUR}{x:,.2f}"


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


# ---------------------------------------------------------------------- main
def main():
    init_db()
    if "user" not in st.session_state:
        login_screen()
        return
    user = st.session_state.user
    st.sidebar.title("💰 Pigmy")
    st.sidebar.caption(f"{user['name']} · {user['role']}")
    pages = {"Collect": lambda: page_collect(user), "Customers & Accounts": lambda: page_customers(user)}
    if user["role"] == "admin":
        pages = {"Dashboard": page_dashboard, **pages, "Payouts": lambda: page_payouts(user),
                 "Agents": page_agents, "Reports": page_reports}
    choice = st.sidebar.radio("Menu", list(pages))
    if st.sidebar.button("Logout"):
        del st.session_state.user
        st.rerun()
    pages[choice]()


main()
