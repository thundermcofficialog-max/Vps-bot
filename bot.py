"""
VPS Manager Discord Bot — single file version.
Run: python bot.py   (after filling .env)
Requires: pip install discord.py python-dotenv
"""

import os
import time
import sqlite3
import contextlib
import subprocess
import secrets
import string

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# ---------------- Config (from .env) ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
ADMIN_LOG_CHANNEL_ID = int(os.getenv("ADMIN_LOG_CHANNEL_ID", "0"))
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "vpsbot.db")
MIN_ACCOUNT_AGE_DAYS = int(os.getenv("MIN_ACCOUNT_AGE_DAYS", "90"))
LXC_BIN = os.getenv("LXC_BIN", "lxc")

EMBED_COLOR = 0x5865F2
EMBED_COLOR_SUCCESS = 0x57F287
EMBED_COLOR_ERROR = 0xED4245
EMBED_COLOR_WARNING = 0xFEE75C

LEVELS = {"support": 1, "moderator": 2, "super_admin": 3}

# ---------------- Database ----------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    invites INTEGER DEFAULT 0,
    joined_at INTEGER,
    account_created_at INTEGER,
    verified INTEGER DEFAULT 0,
    blacklisted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plans (
    plan_name TEXT PRIMARY KEY,
    invites_required INTEGER,
    ram_mb INTEGER,
    cpu_cores INTEGER,
    disk_gb INTEGER,
    duration_days INTEGER,
    allowed_node TEXT
);
CREATE TABLE IF NOT EXISTS nodes (
    node_name TEXT PRIMARY KEY,
    ip TEXT,
    max_slots INTEGER,
    used_slots INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS containers (
    container_id TEXT PRIMARY KEY,
    user_id INTEGER,
    plan_name TEXT,
    node_name TEXT,
    status TEXT DEFAULT 'pending',
    created_at INTEGER,
    expires_at INTEGER,
    ssh_port INTEGER
);
CREATE TABLE IF NOT EXISTS pending_requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    request_type TEXT,
    plan_name TEXT,
    extra TEXT,
    status TEXT DEFAULT 'pending',
    created_at INTEGER,
    handled_by INTEGER
);
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    level TEXT,
    added_by INTEGER,
    added_at INTEGER
);
CREATE TABLE IF NOT EXISTS blacklist (
    user_id INTEGER PRIMARY KEY,
    reason TEXT,
    added_by INTEGER,
    added_at INTEGER
);
CREATE TABLE IF NOT EXISTS audit_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER,
    action TEXT,
    target TEXT,
    detail TEXT,
    created_at INTEGER
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with contextlib.closing(get_conn()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def q(sql, params=(), fetch=None):
    with contextlib.closing(get_conn()) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        if fetch == "one":
            row = cur.fetchone()
            return dict(row) if row else None
        if fetch == "all":
            return [dict(r) for r in cur.fetchall()]
        return cur.lastrowid


def log_action(actor_id, action, target="", detail=""):
    q("INSERT INTO audit_log (actor_id, action, target, detail, created_at) VALUES (?,?,?,?,?)",
      (actor_id, action, str(target), detail, int(time.time())))


# ---------------- LXC helpers (admin-triggered only) ----------------

def run_lxc(args, timeout=60):
    result = subprocess.run([LXC_BIN] + args, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def gen_password(length=16):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def lxc_create(name, image_alias, ram_mb, cpu_cores, disk_gb):
    code, out, err = run_lxc(["launch", f"local:{image_alias}", name])
    if code != 0:
        raise RuntimeError(err)
    run_lxc(["config", "set", name, "limits.memory", f"{ram_mb}MB"])
    run_lxc(["config", "set", name, "limits.cpu", str(cpu_cores)])
    run_lxc(["config", "device", "override", name, "root", f"size={disk_gb}GB"])


def lxc_set_root_password(name):
    password = gen_password()
    run_lxc(["exec", name, "--", "bash", "-c", f"echo 'root:{password}' | chpasswd"])
    return password


def lxc_enable_ssh(name):
    run_lxc(["exec", name, "--", "bash", "-c",
             "apt-get update -y && apt-get install -y openssh-server && "
             "sed -i 's/PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && service ssh restart"])


def lxc_start(name): run_lxc(["start", name])
def lxc_stop(name): run_lxc(["stop", name, "--force"])
def lxc_restart(name): run_lxc(["restart", name, "--force"])
def lxc_delete(name): run_lxc(["delete", name, "--force"])


def lxc_add_port(name, listen_port, container_port, proto="tcp"):
    run_lxc(["config", "device", "add", name, f"port{listen_port}", "proxy",
             f"listen={proto}:0.0.0.0:{listen_port}", f"connect={proto}:127.0.0.1:{container_port}"])


def lxc_remove_port(name, listen_port):
    run_lxc(["config", "device", "remove", name, f"port{listen_port}"])


# ---------------- Permissions ----------------

def admin_level(user_id):
    if user_id == OWNER_ID:
        return "super_admin"
    row = q("SELECT level FROM admins WHERE user_id=?", (user_id,), fetch="one")
    return row["level"] if row else None


def has_level(user_id, required):
    lvl = admin_level(user_id)
    if lvl is None:
        return False
    return LEVELS.get(lvl, 0) >= LEVELS.get(required, 999)


# ---------------- Embeds ----------------

def emb(title, desc="", color=EMBED_COLOR):
    e = discord.Embed(title=title, description=desc, color=color, timestamp=discord.utils.utcnow())
    e.set_footer(text="VPS Manager Bot")
    return e


def emb_ok(title, desc=""): return emb(f"✅ {title}", desc, EMBED_COLOR_SUCCESS)
def emb_err(title, desc=""): return emb(f"❌ {title}", desc, EMBED_COLOR_ERROR)
def emb_warn(title, desc=""): return emb(f"⚠️ {title}", desc, EMBED_COLOR_WARNING)


# ---------------- Bot setup ----------------

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    print(f"Logged in as {bot.user} ({bot.user.id})")


def admin_required(level):
    async def predicate(interaction: discord.Interaction):
        if not has_level(interaction.user.id, level):
            await interaction.response.send_message(embed=emb_err("No permission", f"Requires `{level}` level."), ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


# =====================================================================
# USER COMMANDS
# =====================================================================

@bot.tree.command(name="verify", description="Verify your account (checks account age)")
async def verify(interaction: discord.Interaction):
    user = interaction.user
    age_days = (discord.utils.utcnow() - user.created_at).days
    if age_days < MIN_ACCOUNT_AGE_DAYS:
        await interaction.response.send_message(
            embed=emb_err("Verification failed", f"Account age {age_days}d < required {MIN_ACCOUNT_AGE_DAYS}d. Possible alt account."),
            ephemeral=True)
        return
    q("INSERT INTO users (user_id, joined_at, account_created_at, verified) VALUES (?,?,?,1) "
      "ON CONFLICT(user_id) DO UPDATE SET verified=1",
      (user.id, int(time.time()), int(user.created_at.timestamp())))
    await interaction.response.send_message(embed=emb_ok("Verified", "You can now use redeem/renew commands."), ephemeral=True)


@bot.tree.command(name="invites", description="Check your invite count")
async def invites(interaction: discord.Interaction):
    row = q("SELECT invites FROM users WHERE user_id=?", (interaction.user.id,), fetch="one")
    count = row["invites"] if row else 0
    await interaction.response.send_message(embed=emb("Your invites", f"You have **{count}** invites."))


@bot.tree.command(name="leaderboard", description="Top inviters")
async def leaderboard(interaction: discord.Interaction):
    rows = q("SELECT * FROM users ORDER BY invites DESC LIMIT 10", fetch="all")
    desc = "\n".join(f"{i+1}. <@{r['user_id']}> — {r['invites']} invites" for i, r in enumerate(rows)) or "No data yet."
    await interaction.response.send_message(embed=emb("🏆 Invite Leaderboard", desc))


@bot.tree.command(name="plans", description="List available VPS plans")
async def plans(interaction: discord.Interaction):
    rows = q("SELECT * FROM plans", fetch="all")
    if not rows:
        await interaction.response.send_message(embed=emb_warn("No plans", "No plans configured yet."))
        return
    desc = ""
    for p in rows:
        desc += (f"**{p['plan_name']}** — {p['invites_required']} invites | "
                 f"{p['ram_mb']}MB RAM | {p['cpu_cores']} vCPU | {p['disk_gb']}GB disk | {p['duration_days']}d\n")
    await interaction.response.send_message(embed=emb("📦 Available Plans", desc))


@bot.tree.command(name="redeem", description="Request a VPS plan using your invites")
@app_commands.describe(plan_name="Plan name to redeem")
async def redeem(interaction: discord.Interaction, plan_name: str):
    user_id = interaction.user.id
    bl = q("SELECT 1 FROM blacklist WHERE user_id=?", (user_id,), fetch="one")
    if bl:
        await interaction.response.send_message(embed=emb_err("Blocked", "You are blacklisted."), ephemeral=True)
        return

    plan = q("SELECT * FROM plans WHERE plan_name=?", (plan_name,), fetch="one")
    if not plan:
        await interaction.response.send_message(embed=emb_err("Unknown plan", f"No plan named `{plan_name}`."), ephemeral=True)
        return

    user = q("SELECT * FROM users WHERE user_id=?", (user_id,), fetch="one")
    invites_count = user["invites"] if user else 0
    if invites_count < plan["invites_required"]:
        await interaction.response.send_message(
            embed=emb_err("Insufficient invites",
                           f"{invites_count}/{plan['invites_required']} required for `{plan_name}`."),
            ephemeral=True)
        return

    req_id = q("INSERT INTO pending_requests (user_id, request_type, plan_name, created_at) VALUES (?,?,?,?)",
               (user_id, "redeem", plan_name, int(time.time())))

    await interaction.response.send_message(
        embed=emb_ok("Request submitted", f"Redeem request for `{plan_name}` sent for admin approval (ID `{req_id}`)."),
        ephemeral=True)

    if ADMIN_LOG_CHANNEL_ID:
        ch = bot.get_channel(ADMIN_LOG_CHANNEL_ID)
        if ch:
            await ch.send(embed=emb_warn("New redeem request",
                                          f"User: <@{user_id}>\nPlan: `{plan_name}`\nInvites: {invites_count}/{plan['invites_required']}\n"
                                          f"Approve with `/approve request_id:{req_id}`"))


@bot.tree.command(name="renew", description="Request renewal of your current VPS using invites")
async def renew(interaction: discord.Interaction):
    user_id = interaction.user.id
    container = q("SELECT * FROM containers WHERE user_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
                   (user_id,), fetch="one")
    if not container:
        await interaction.response.send_message(embed=emb_err("No active VPS", "You don't have an active VPS to renew."), ephemeral=True)
        return

    req_id = q("INSERT INTO pending_requests (user_id, request_type, plan_name, created_at) VALUES (?,?,?,?)",
               (user_id, "renew", container["plan_name"], int(time.time())))
    await interaction.response.send_message(embed=emb_ok("Renewal requested", f"Request ID `{req_id}` sent for admin approval."), ephemeral=True)

    if ADMIN_LOG_CHANNEL_ID:
        ch = bot.get_channel(ADMIN_LOG_CHANNEL_ID)
        if ch:
            await ch.send(embed=emb_warn("Renewal request", f"User: <@{user_id}>\nContainer: `{container['container_id']}`\n"
                                          f"Approve with `/approve request_id:{req_id}`"))


@bot.tree.command(name="status", description="Check your VPS status")
async def status(interaction: discord.Interaction):
    c = q("SELECT * FROM containers WHERE user_id=? ORDER BY created_at DESC LIMIT 1", (interaction.user.id,), fetch="one")
    if not c:
        await interaction.response.send_message(embed=emb_warn("No VPS", "You don't have a VPS yet. Use /redeem."), ephemeral=True)
        return
    expires = time.strftime("%Y-%m-%d", time.localtime(c["expires_at"])) if c["expires_at"] else "N/A"
    await interaction.response.send_message(embed=emb("Your VPS", f"ID: `{c['container_id']}`\nPlan: `{c['plan_name']}`\n"
                                                        f"Status: `{c['status']}`\nExpires: {expires}"), ephemeral=True)


@bot.tree.command(name="vps_info", description="Show resource info of your VPS")
async def vps_info(interaction: discord.Interaction):
    c = q("SELECT * FROM containers WHERE user_id=? AND status='active'", (interaction.user.id,), fetch="one")
    if not c:
        await interaction.response.send_message(embed=emb_err("No active VPS", ""), ephemeral=True)
        return
    code, out, err = run_lxc(["info", c["container_id"]])
    await interaction.response.send_message(embed=emb("VPS Info", f"```{out[:1500] or err}```"), ephemeral=True)


@bot.tree.command(name="vps_reboot", description="Reboot your VPS")
async def vps_reboot(interaction: discord.Interaction):
    c = q("SELECT * FROM containers WHERE user_id=? AND status='active'", (interaction.user.id,), fetch="one")
    if not c:
        await interaction.response.send_message(embed=emb_err("No active VPS", ""), ephemeral=True)
        return
    lxc_restart(c["container_id"])
    log_action(interaction.user.id, "vps_reboot", c["container_id"])
    await interaction.response.send_message(embed=emb_ok("Rebooted", f"`{c['container_id']}` is restarting."), ephemeral=True)


@bot.tree.command(name="vps_password_reset", description="Reset your VPS root password")
async def vps_password_reset(interaction: discord.Interaction):
    c = q("SELECT * FROM containers WHERE user_id=? AND status='active'", (interaction.user.id,), fetch="one")
    if not c:
        await interaction.response.send_message(embed=emb_err("No active VPS", ""), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    new_pass = lxc_set_root_password(c["container_id"])
    log_action(interaction.user.id, "password_reset", c["container_id"])
    try:
        await interaction.user.send(embed=emb_ok("New root password", f"Container: `{c['container_id']}`\nPassword: `{new_pass}`"))
        await interaction.followup.send(embed=emb_ok("Sent", "Check your DMs."), ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(embed=emb_err("DM failed", "Enable DMs from server members and try again."), ephemeral=True)


@bot.tree.command(name="vps_ports", description="List your VPS's forwarded ports")
async def vps_ports(interaction: discord.Interaction):
    c = q("SELECT * FROM containers WHERE user_id=? AND status='active'", (interaction.user.id,), fetch="one")
    if not c:
        await interaction.response.send_message(embed=emb_err("No active VPS", ""), ephemeral=True)
        return
    code, out, err = run_lxc(["config", "device", "list", c["container_id"]])
    ports = [l for l in out.splitlines() if l.startswith("port")]
    await interaction.response.send_message(embed=emb("Forwarded ports", "\n".join(ports) or "None"), ephemeral=True)


@bot.tree.command(name="vps_requestport", description="Request a new port forward (needs admin approval)")
@app_commands.describe(port="Port number to forward")
async def vps_requestport(interaction: discord.Interaction, port: int):
    c = q("SELECT * FROM containers WHERE user_id=? AND status='active'", (interaction.user.id,), fetch="one")
    if not c:
        await interaction.response.send_message(embed=emb_err("No active VPS", ""), ephemeral=True)
        return
    req_id = q("INSERT INTO pending_requests (user_id, request_type, extra, created_at) VALUES (?,?,?,?)",
               (interaction.user.id, "port", str(port), int(time.time())))
    await interaction.response.send_message(embed=emb_ok("Port request submitted", f"Request ID `{req_id}` pending admin approval."), ephemeral=True)
    if ADMIN_LOG_CHANNEL_ID:
        ch = bot.get_channel(ADMIN_LOG_CHANNEL_ID)
        if ch:
            await ch.send(embed=emb_warn("Port forward request", f"User: <@{interaction.user.id}>\nContainer: `{c['container_id']}`\n"
                                          f"Port: {port}\nApprove with `/approve request_id:{req_id}`"))


@bot.tree.command(name="support", description="Open a support ticket")
@app_commands.describe(message="Describe your issue")
async def support(interaction: discord.Interaction, message: str):
    log_action(interaction.user.id, "support_ticket", detail=message)
    if ADMIN_LOG_CHANNEL_ID:
        ch = bot.get_channel(ADMIN_LOG_CHANNEL_ID)
        if ch:
            await ch.send(embed=emb_warn("Support ticket", f"From: <@{interaction.user.id}>\n{message}"))
    await interaction.response.send_message(embed=emb_ok("Ticket submitted", "An admin will get back to you."), ephemeral=True)


# =====================================================================
# ADMIN COMMANDS
# =====================================================================

@bot.tree.command(name="approve", description="[Admin] Approve a pending request")
@admin_required("moderator")
@app_commands.describe(request_id="ID of the request to approve")
async def approve(interaction: discord.Interaction, request_id: int):
    req = q("SELECT * FROM pending_requests WHERE request_id=?", (request_id,), fetch="one")
    if not req or req["status"] != "pending":
        await interaction.response.send_message(embed=emb_err("Not found", "No pending request with that ID."), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    user_id = req["user_id"]

    if req["request_type"] == "redeem":
        plan = q("SELECT * FROM plans WHERE plan_name=?", (req["plan_name"],), fetch="one")
        node = q("SELECT * FROM nodes WHERE status='active' AND used_slots < max_slots LIMIT 1", fetch="one")
        if not node:
            await interaction.followup.send(embed=emb_err("No capacity", "All nodes are full."), ephemeral=True)
            return
        container_id = f"vps-{user_id}-{int(time.time())}"
        try:
            lxc_create(container_id, "ubuntu2204", plan["ram_mb"], plan["cpu_cores"], plan["disk_gb"])
            lxc_enable_ssh(container_id)
            password = lxc_set_root_password(container_id)
        except Exception as e:
            await interaction.followup.send(embed=emb_err("Provisioning failed", str(e)), ephemeral=True)
            return
        expires_at = int(time.time()) + plan["duration_days"] * 86400
        q("INSERT INTO containers (container_id, user_id, plan_name, node_name, status, created_at, expires_at) "
          "VALUES (?,?,?,?, 'active', ?, ?)",
          (container_id, user_id, req["plan_name"], node["node_name"], int(time.time()), expires_at))
        q("UPDATE nodes SET used_slots = used_slots + 1 WHERE node_name=?", (node["node_name"],))
        q("UPDATE pending_requests SET status='approved', handled_by=? WHERE request_id=?", (interaction.user.id, request_id))
        log_action(interaction.user.id, "approve_redeem", container_id)

        member = await bot.fetch_user(user_id)
        try:
            await member.send(embed=emb_ok("Your VPS is ready!",
                                            f"Container: `{container_id}`\nRoot password: `{password}`\n"
                                            f"IP: `{node['ip']}`\nUse `/vps_info` for more details."))
        except discord.Forbidden:
            pass
        await interaction.followup.send(embed=emb_ok("Approved", f"Provisioned `{container_id}` for <@{user_id}>."), ephemeral=True)

    elif req["request_type"] == "renew":
        container = q("SELECT * FROM containers WHERE user_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
                       (user_id,), fetch="one")
        plan = q("SELECT * FROM plans WHERE plan_name=?", (req["plan_name"],), fetch="one")
        new_expiry = int(time.time()) + plan["duration_days"] * 86400
        q("UPDATE containers SET expires_at=? WHERE container_id=?", (new_expiry, container["container_id"]))
        q("UPDATE pending_requests SET status='approved', handled_by=? WHERE request_id=?", (interaction.user.id, request_id))
        log_action(interaction.user.id, "approve_renew", container["container_id"])
        await interaction.followup.send(embed=emb_ok("Renewed", f"Extended `{container['container_id']}`."), ephemeral=True)

    elif req["request_type"] == "port":
        container = q("SELECT * FROM containers WHERE user_id=? AND status='active'", (user_id,), fetch="one")
        port = int(req["extra"])
        lxc_add_port(container["container_id"], port, port)
        q("UPDATE pending_requests SET status='approved', handled_by=? WHERE request_id=?", (interaction.user.id, request_id))
        log_action(interaction.user.id, "approve_port", container["container_id"], f"port={port}")
        await interaction.followup.send(embed=emb_ok("Port opened", f"Port {port} forwarded on `{container['container_id']}`."), ephemeral=True)

    else:
        await interaction.followup.send(embed=emb_err("Unsupported type", req["request_type"]), ephemeral=True)


@bot.tree.command(name="reject", description="[Admin] Reject a pending request")
@admin_required("moderator")
@app_commands.describe(request_id="ID of the request", reason="Reason for rejection")
async def reject(interaction: discord.Interaction, request_id: int, reason: str = "Not specified"):
    q("UPDATE pending_requests SET status='rejected', handled_by=? WHERE request_id=?", (interaction.user.id, request_id))
    log_action(interaction.user.id, "reject_request", request_id, reason)
    await interaction.response.send_message(embed=emb_ok("Rejected", f"Request `{request_id}` rejected."), ephemeral=True)


@bot.tree.command(name="plan_add", description="[Admin] Add or update a plan")
@admin_required("super_admin")
@app_commands.describe(name="Plan name", invites_required="Invites needed", ram_mb="RAM in MB",
                        cpu_cores="vCPU cores", disk_gb="Disk in GB", duration_days="Duration in days")
async def plan_add(interaction: discord.Interaction, name: str, invites_required: int, ram_mb: int,
                    cpu_cores: int, disk_gb: int, duration_days: int):
    q("INSERT INTO plans (plan_name, invites_required, ram_mb, cpu_cores, disk_gb, duration_days) VALUES (?,?,?,?,?,?) "
      "ON CONFLICT(plan_name) DO UPDATE SET invites_required=excluded.invites_required, ram_mb=excluded.ram_mb, "
      "cpu_cores=excluded.cpu_cores, disk_gb=excluded.disk_gb, duration_days=excluded.duration_days",
      (name, invites_required, ram_mb, cpu_cores, disk_gb, duration_days))
    log_action(interaction.user.id, "plan_add", name)
    await interaction.response.send_message(embed=emb_ok("Plan saved", f"`{name}`: {ram_mb}MB RAM, {cpu_cores} vCPU, {disk_gb}GB disk."), ephemeral=True)


@bot.tree.command(name="plan_remove", description="[Admin] Remove a plan")
@admin_required("super_admin")
async def plan_remove(interaction: discord.Interaction, name: str):
    q("DELETE FROM plans WHERE plan_name=?", (name,))
    log_action(interaction.user.id, "plan_remove", name)
    await interaction.response.send_message(embed=emb_ok("Plan removed", name), ephemeral=True)


@bot.tree.command(name="node_add", description="[Admin] Register a new node")
@admin_required("super_admin")
@app_commands.describe(name="Node name", ip="Node IP", max_slots="Max containers this node can host")
async def node_add(interaction: discord.Interaction, name: str, ip: str, max_slots: int):
    q("INSERT INTO nodes (node_name, ip, max_slots) VALUES (?,?,?) "
      "ON CONFLICT(node_name) DO UPDATE SET ip=excluded.ip, max_slots=excluded.max_slots", (name, ip, max_slots))
    log_action(interaction.user.id, "node_add", name)
    await interaction.response.send_message(embed=emb_ok("Node added", f"`{name}` ({ip}) — {max_slots} slots."), ephemeral=True)


@bot.tree.command(name="node_remove", description="[Admin] Remove a node")
@admin_required("super_admin")
async def node_remove(interaction: discord.Interaction, name: str):
    q("DELETE FROM nodes WHERE node_name=?", (name,))
    log_action(interaction.user.id, "node_remove", name)
    await interaction.response.send_message(embed=emb_ok("Node removed", name), ephemeral=True)


@bot.tree.command(name="node_list", description="[Admin] List all nodes and their slot usage")
@admin_required("support")
async def node_list(interaction: discord.Interaction):
    rows = q("SELECT * FROM nodes", fetch="all")
    desc = "\n".join(f"**{n['node_name']}** ({n['ip']}) — {n['used_slots']}/{n['max_slots']} slots — `{n['status']}`" for n in rows) or "No nodes."
    await interaction.response.send_message(embed=emb("Nodes", desc), ephemeral=True)


@bot.tree.command(name="node_disable", description="[Admin] Disable a node (no new assignments)")
@admin_required("moderator")
async def node_disable(interaction: discord.Interaction, name: str):
    q("UPDATE nodes SET status='disabled' WHERE node_name=?", (name,))
    log_action(interaction.user.id, "node_disable", name)
    await interaction.response.send_message(embed=emb_ok("Node disabled", name), ephemeral=True)


@bot.tree.command(name="node_enable", description="[Admin] Re-enable a node")
@admin_required("moderator")
async def node_enable(interaction: discord.Interaction, name: str):
    q("UPDATE nodes SET status='active' WHERE node_name=?", (name,))
    log_action(interaction.user.id, "node_enable", name)
    await interaction.response.send_message(embed=emb_ok("Node enabled", name), ephemeral=True)


@bot.tree.command(name="admin_add", description="[Owner] Add a new admin")
@admin_required("super_admin")
@app_commands.describe(user="User to promote", level="support / moderator / super_admin")
async def admin_add(interaction: discord.Interaction, user: discord.User, level: str):
    if level not in LEVELS:
        await interaction.response.send_message(embed=emb_err("Invalid level", "Use support, moderator, or super_admin."), ephemeral=True)
        return
    q("INSERT INTO admins (user_id, level, added_by, added_at) VALUES (?,?,?,?) "
      "ON CONFLICT(user_id) DO UPDATE SET level=excluded.level", (user.id, level, interaction.user.id, int(time.time())))
    log_action(interaction.user.id, "admin_add", user.id, level)
    await interaction.response.send_message(embed=emb_ok("Admin added", f"<@{user.id}> is now `{level}`."), ephemeral=True)


@bot.tree.command(name="admin_remove", description="[Owner] Remove an admin")
@admin_required("super_admin")
async def admin_remove(interaction: discord.Interaction, user: discord.User):
    q("DELETE FROM admins WHERE user_id=?", (user.id,))
    log_action(interaction.user.id, "admin_remove", user.id)
    await interaction.response.send_message(embed=emb_ok("Admin removed", f"<@{user.id}>"), ephemeral=True)


@bot.tree.command(name="admin_list", description="[Admin] List all admins")
@admin_required("support")
async def admin_list(interaction: discord.Interaction):
    rows = q("SELECT * FROM admins", fetch="all")
    desc = "\n".join(f"<@{a['user_id']}> — `{a['level']}`" for a in rows) or "No admins added (owner is always super_admin)."
    await interaction.response.send_message(embed=emb("Admins", desc), ephemeral=True)


@bot.tree.command(name="blacklist_add", description="[Admin] Blacklist a user")
@admin_required("moderator")
async def blacklist_add(interaction: discord.Interaction, user: discord.User, reason: str = "Not specified"):
    q("INSERT INTO blacklist (user_id, reason, added_by, added_at) VALUES (?,?,?,?) "
      "ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason", (user.id, reason, interaction.user.id, int(time.time())))
    log_action(interaction.user.id, "blacklist_add", user.id, reason)
    await interaction.response.send_message(embed=emb_ok("Blacklisted", f"<@{user.id}> — {reason}"), ephemeral=True)


@bot.tree.command(name="blacklist_remove", description="[Admin] Remove a user from blacklist")
@admin_required("moderator")
async def blacklist_remove(interaction: discord.Interaction, user: discord.User):
    q("DELETE FROM blacklist WHERE user_id=?", (user.id,))
    log_action(interaction.user.id, "blacklist_remove", user.id)
    await interaction.response.send_message(embed=emb_ok("Removed from blacklist", f"<@{user.id}>"), ephemeral=True)


@bot.tree.command(name="user_info", description="[Admin] Show full info about a user")
@admin_required("support")
async def user_info(interaction: discord.Interaction, user: discord.User):
    u = q("SELECT * FROM users WHERE user_id=?", (user.id,), fetch="one")
    c = q("SELECT * FROM containers WHERE user_id=? ORDER BY created_at DESC LIMIT 1", (user.id,), fetch="one")
    desc = f"Invites: {u['invites'] if u else 0}\n"
    if c:
        desc += f"Container: `{c['container_id']}` ({c['status']})\nPlan: `{c['plan_name']}`"
    else:
        desc += "No container."
    await interaction.response.send_message(embed=emb(f"Info: {user}", desc), ephemeral=True)


@bot.tree.command(name="audit", description="[Admin] Show audit log for a user or container")
@admin_required("moderator")
async def audit(interaction: discord.Interaction, target: str):
    rows = q("SELECT * FROM audit_log WHERE target=? ORDER BY created_at DESC LIMIT 20", (target,), fetch="all")
    desc = "\n".join(f"<t:{r['created_at']}:R> `{r['action']}` by <@{r['actor_id']}> — {r['detail']}" for r in rows) or "No entries."
    await interaction.response.send_message(embed=emb("Audit log", desc), ephemeral=True)


@bot.tree.command(name="announcement", description="[Admin] Broadcast a message")
@admin_required("moderator")
async def announcement(interaction: discord.Interaction, message: str):
    ch = bot.get_channel(ANNOUNCE_CHANNEL_ID)
    if ch:
        await ch.send(embed=emb("📢 Announcement", message))
    await interaction.response.send_message(embed=emb_ok("Sent", ""), ephemeral=True)


@bot.tree.command(name="container_stop", description="[Admin] Stop a container")
@admin_required("moderator")
async def container_stop(interaction: discord.Interaction, container_id: str):
    lxc_stop(container_id)
    q("UPDATE containers SET status='stopped' WHERE container_id=?", (container_id,))
    log_action(interaction.user.id, "container_stop", container_id)
    await interaction.response.send_message(embed=emb_ok("Stopped", container_id), ephemeral=True)


@bot.tree.command(name="container_start", description="[Admin] Start a container")
@admin_required("moderator")
async def container_start(interaction: discord.Interaction, container_id: str):
    lxc_start(container_id)
    q("UPDATE containers SET status='active' WHERE container_id=?", (container_id,))
    log_action(interaction.user.id, "container_start", container_id)
    await interaction.response.send_message(embed=emb_ok("Started", container_id), ephemeral=True)


@bot.tree.command(name="container_delete", description="[Admin] Permanently delete a container")
@admin_required("super_admin")
async def container_delete(interaction: discord.Interaction, container_id: str):
    lxc_delete(container_id)
    row = q("SELECT node_name FROM containers WHERE container_id=?", (container_id,), fetch="one")
    if row:
        q("UPDATE nodes SET used_slots = used_slots - 1 WHERE node_name=?", (row["node_name"],))
    q("UPDATE containers SET status='deleted' WHERE container_id=?", (container_id,))
    log_action(interaction.user.id, "container_delete", container_id)
    await interaction.response.send_message(embed=emb_ok("Deleted", container_id), ephemeral=True)


@bot.tree.command(name="add_invites", description="[Admin] Manually add invites to a user")
@admin_required("moderator")
async def add_invites_cmd(interaction: discord.Interaction, user: discord.User, amount: int):
    q("INSERT INTO users (user_id, invites, joined_at, account_created_at) VALUES (?,?,?,?) "
      "ON CONFLICT(user_id) DO UPDATE SET invites = invites + ?",
      (user.id, amount, int(time.time()), int(user.created_at.timestamp()), amount))
    log_action(interaction.user.id, "add_invites", user.id, str(amount))
    await interaction.response.send_message(embed=emb_ok("Invites updated", f"<@{user.id}> +{amount}"), ephemeral=True)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing — fill it in .env")
    init_db()
    bot.run(BOT_TOKEN)
