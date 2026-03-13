"""
bot.py — Malice
Asmodeus's fan economy bot for Devil's Den.

Guild:    1391928616098467960
Log:      944764052062744656
Admins:   860562689704329236 (Asmodeus)
          1465880892110143570 (Asmodeus alt)
          448896936481652777 (Guy)

Fulfillment
───────────
  auto   — purchase confirmed instantly via ephemeral, logged silently to LOG_GUILD
  manual — purchase posts to REQUESTS_CH for Asmodeus to action via buttons
"""

import os
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")

import database as db
import embeds as em

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ── Config ────────────────────────────────────────────────────────────────────
GUILD_ID    = 1375155577977704588
LOG_GUILD   = 944764052062744656
LOG_CH      = 944766955586482207   # specific channel in LOG_GUILD for purchase logs
DROPS_CH    = 1382900600802381955
REQUESTS_CH   = 1481377615154512033
QUEUE_ALERT_CH = 1481377615154512033
FULFILLMENT_LOG_CH = 1478253892306210859  # channel to announce fulfillment mode changes
ADMIN_IDS   = {860562689704329236, 1465880892110143570, 448896936481652777, 182725383609778176}

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents         = discord.Intents.default()
intents.members = True


class MaliceBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)


bot  = MaliceBot()
tree = bot.tree


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def require_admin(interaction: discord.Interaction) -> bool:
    return is_admin(interaction.user.id)


DEV_ID = 448896936481652777

async def check_banned(interaction: discord.Interaction, command: str = None) -> bool:
    """Returns True (and sends error) if the user is banned. Use as early guard in commands.
    Admins and the dev ID are always exempt."""
    if is_admin(interaction.user.id) or interaction.user.id == DEV_ID:
        return False
    if db.is_banned(str(interaction.user.id), command):
        await interaction.response.send_message(
            "You are not permitted to use this. 🖤", ephemeral=True
        )
        return True
    return False


async def ensure_user(interaction: discord.Interaction):
    return db.get_or_create_user(
        str(interaction.user.id), interaction.user.display_name
    )


async def get_requests_channel(guild: discord.Guild):
    if not REQUESTS_CH:
        return None
    return guild.get_channel(REQUESTS_CH)


async def get_drops_channel(guild: discord.Guild):
    if not DROPS_CH:
        return None
    return guild.get_channel(DROPS_CH)


async def get_log_channel(guild: discord.Guild):
    """Returns the designated purchase log channel in LOG_GUILD."""
    log_guild = bot.get_guild(LOG_GUILD)
    if not log_guild:
        return None
    return log_guild.get_channel(LOG_CH)


async def log_purchase(buyer: discord.Member, item, currency: str, fulfillment: str):
    """Silently logs every purchase to LOG_GUILD."""
    try:
        log_ch = await get_log_channel(buyer.guild)
        if not log_ch:
            return
        cur_emote = em._cur(currency)
        e = discord.Embed(color=0x1a0a0a)
        e.add_field(name="User",        value=f"{buyer} (`{buyer.id}`)", inline=True)
        e.add_field(name="Item",        value=f"{item['emoji']} {item['name']}", inline=True)
        e.add_field(name="Price",       value=f"{cur_emote} {item['price']} {currency}", inline=True)
        e.add_field(name="Fulfillment", value=fulfillment, inline=True)
        e.add_field(name="Guild",       value=buyer.guild.name, inline=True)
        e.timestamp = datetime.now(EST)
        e.set_footer(text="malice · purchase log")
        await log_ch.send(embed=e)
    except Exception:
        pass


def _cur(c):
    return em._cur(c)


CURRENCY_CHOICES = [
    app_commands.Choice(name="blood", value="blood"),
    app_commands.Choice(name="hex",   value="hex"),
]
ALL_CURRENCY_CHOICES = [
    app_commands.Choice(name="blood", value="blood"),
    app_commands.Choice(name="hex",   value="hex"),
    app_commands.Choice(name="void",  value="void"),
]

# ── Ready ──────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    db.init_db()
    print(f"😈 Malice online as {bot.user}")
    if DROPS_CH:
        drop_ticker.start()
    order_expiry_ticker.start()


# ─────────────────────────────────────────────────────────────────────────────
#  PURCHASE VIEW
# ─────────────────────────────────────────────────────────────────────────────

class PurchaseView(discord.ui.View):
    def __init__(self, user_id: str, item_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.item_id = item_id

    @discord.ui.button(label="✅ Confirm Offering", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("Not your offering.", ephemeral=True)
            return

        ok, msg, fulfillment = db.purchase_item(self.user_id, self.item_id)
        if not ok:
            await interaction.response.edit_message(embed=em.error_embed(msg), view=None)
            return

        item = db.get_item(self.item_id)
        user = db.get_user(self.user_id)
        currency = "hex" if item["nsfw"] else (item["price_currency"] or "blood")

        # Silent purchase log
        await log_purchase(interaction.user, item, currency, fulfillment)

        if fulfillment == "manual":
            # Check global queue limit before accepting
            open_count = db.get_open_manual_order_count()
            if open_count >= db.MANUAL_QUEUE_LIMIT:
                # Refund the purchase
                db.update_currency(
                    self.user_id, currency, item["price"],
                    f"Refund — queue full: {item['name']}", "refund"
                )
                self.stop()
                await interaction.response.edit_message(
                    embed=em.error_embed(
                        f"The request queue is currently full ({db.MANUAL_QUEUE_LIMIT} orders in progress). "
                        f"Your **{item['price']} {currency}** has been refunded. Try again later. 🖤"
                    ),
                    view=None
                )
                return

            # Post to requests channel for Asmodeus to action
            purchase_id = db.get_last_purchase_id(self.user_id)
            if purchase_id:
                order_id = db.create_order(purchase_id, self.user_id, item)
                guild    = interaction.guild
                if guild:
                    requests_ch = await get_requests_channel(guild)
                    if requests_ch:
                        buyer      = guild.get_member(int(self.user_id))
                        order      = db.get_order(order_id)
                        order_view = OrderView(order_id)
                        sent       = await requests_ch.send(
                            embed=em.order_request_embed(order, buyer),
                            view=order_view
                        )
                        db.set_order_message_id(order_id, str(sent.id))

                    # Alert if queue just hit the limit
                    new_count = db.get_open_manual_order_count()
                    if new_count >= db.MANUAL_QUEUE_LIMIT and QUEUE_ALERT_CH:
                        alert_ch = guild.get_channel(QUEUE_ALERT_CH)
                        if alert_ch:
                            try:
                                await alert_ch.send(
                                    embed=discord.Embed(
                                        title="📋 Request Queue Full",
                                        description=f"The manual request queue has reached **{db.MANUAL_QUEUE_LIMIT} orders**. No new manual purchases will be accepted until orders are completed.",
                                        color=em.COLOR_DANGER
                                    )
                                )
                            except Exception:
                                pass

        new_tier = db.check_and_grant_tier_reward(self.user_id)
        self.stop()

        # Auto: confirm only. Manual: confirm + note it needs fulfilling.
        embed = em.purchase_success_embed(item, user, fulfillment)
        await interaction.response.edit_message(embed=embed, view=None)

        if new_tier:
            await interaction.followup.send(
                embed=em.tier_up_embed(new_tier, user), ephemeral=True
            )
            await _sync_tier_role(interaction.guild, interaction.user, new_tier)

    @discord.ui.button(label="❌ Retreat", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(embed=em.error_embed("Offering cancelled."), view=None)


# ─────────────────────────────────────────────────────────────────────────────
#  ORDER VIEW (manual items)
# ─────────────────────────────────────────────────────────────────────────────

class OrderView(discord.ui.View):
    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id

    @discord.ui.button(label="✅ Complete", style=discord.ButtonStyle.success)
    async def complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not require_admin(interaction):
            await interaction.response.send_message("Admins only. 😈", ephemeral=True)
            return
        ok, order = db.action_order(self.order_id, "completed", str(interaction.user.id))
        if not ok:
            await interaction.response.send_message(embed=em.error_embed(order), ephemeral=True)
            return

        # Payout — manual complete
        db.process_payout(
            currency    = order["price_currency"],
            price       = order["price"],
            actioned_by = str(interaction.user.id)
        )

        try:
            member = interaction.guild.get_member(int(order["discord_id"]))
            if member:
                await member.send(embed=discord.Embed(
                    description="Your order has been delivered. Check your DMs. 😈",
                    color=em.ORDER_STATUS_COLORS.get("completed", 0x2a4a1a)
                ))
        except Exception:
            pass
        await interaction.response.edit_message(
            embed=em.order_request_embed(db.get_order(self.order_id)), view=self
        )

    @discord.ui.button(label="↩️ Refund", style=discord.ButtonStyle.secondary)
    async def refund(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not require_admin(interaction):
            await interaction.response.send_message("Admins only. 😈", ephemeral=True)
            return
        ok, order = db.action_order(self.order_id, "refunded", str(interaction.user.id))
        if not ok:
            await interaction.response.send_message(embed=em.error_embed(order), ephemeral=True)
            return
        cur = order["price_currency"] or "blood"
        db.update_currency(order["discord_id"], cur, order["price"],
                           f"Refund: {order['item_name']}", "refund")
        try:
            member = interaction.guild.get_member(int(order["discord_id"]))
            if member:
                await member.send(embed=discord.Embed(
                    description=f"Your order for **{order['item_name']}** was refunded. {_cur(cur)} {order['price']} {cur} returned.",
                    color=em.ORDER_STATUS_COLORS.get("refunded", 0x6B2020)
                ))
        except Exception:
            pass
        await interaction.response.edit_message(
            embed=em.order_request_embed(db.get_order(self.order_id)), view=self
        )

    @discord.ui.button(label="❌ Invalid", style=discord.ButtonStyle.danger)
    async def invalid(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not require_admin(interaction):
            await interaction.response.send_message("Admins only. 😈", ephemeral=True)
            return
        ok, order = db.action_order(self.order_id, "invalid", str(interaction.user.id))
        if not ok:
            await interaction.response.send_message(embed=em.error_embed(order), ephemeral=True)
            return
        try:
            member = interaction.guild.get_member(int(order["discord_id"]))
            if member:
                await member.send(embed=discord.Embed(
                    description=f"Your order for **{order['item_name']}** could not be fulfilled. Contact Asmodeus.",
                    color=em.ORDER_STATUS_COLORS.get("invalid", 0xcc2200)
                ))
        except Exception:
            pass
        await interaction.response.edit_message(
            embed=em.order_request_embed(db.get_order(self.order_id)), view=self
        )

    @discord.ui.button(label="📝 Note", style=discord.ButtonStyle.primary)
    async def add_note(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not require_admin(interaction):
            await interaction.response.send_message("Admins only. 😈", ephemeral=True)
            return
        await interaction.response.send_modal(NoteModal(self.order_id))


class NoteModal(discord.ui.Modal, title="Add Order Note"):
    note = discord.ui.TextInput(
        label="Note", placeholder="Add a note...",
        style=discord.TextStyle.paragraph, required=True, max_length=500
    )
    def __init__(self, order_id: int):
        super().__init__()
        self.order_id = order_id

    async def on_submit(self, interaction: discord.Interaction):
        with db.get_conn() as conn:
            conn.execute("UPDATE orders SET note=? WHERE id=?",
                         (self.note.value, self.order_id))
            conn.commit()
        await interaction.response.send_message(
            embed=em.success_embed("Note added."), ephemeral=True
        )


# ─────────────────────────────────────────────────────────────────────────────
#  SUMMON (void forge) VIEW
# ─────────────────────────────────────────────────────────────────────────────

class SummonView(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=60)
        self.user_id = user_id

    @discord.ui.button(label="🖤 Confirm Sacrifice", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("Not your ritual.", ephemeral=True)
            return
        ok, msg = db.forge_void(self.user_id)
        self.stop()
        if ok:
            user = db.get_user(self.user_id)
            await interaction.response.edit_message(embed=em.summon_success_embed(user), view=None)
        else:
            await interaction.response.edit_message(embed=em.error_embed(msg), view=None)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(embed=em.error_embed("Ritual cancelled."), view=None)


# ─────────────────────────────────────────────────────────────────────────────
#  WAITLIST JOIN VIEW
# ─────────────────────────────────────────────────────────────────────────────

class WaitlistJoinView(discord.ui.View):
    def __init__(self, user_id: str, item_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.item_id = item_id

    @discord.ui.button(label="🩸 Mark My Place", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("Not your view.", ephemeral=True)
            return
        ok, msg, position = db.join_waitlist(self.user_id, self.item_id)
        self.stop()
        if not ok:
            await interaction.response.edit_message(embed=em.error_embed(msg), view=None)
            return
        item = db.get_item(self.item_id)
        await interaction.response.edit_message(
            embed=em.success_embed(
                f"Your place for **{item['emoji']} {item['name']}** is marked.\n"
                f"Position: **#{position}**. You'll be notified when it restocks. 😈"
            ),
            view=None
        )


# ─────────────────────────────────────────────────────────────────────────────
#  COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

@tree.command(name="daily", description="Perform your daily ritual — claim blood and hex 🩸")
async def cmd_daily(interaction: discord.Interaction):
    if await check_banned(interaction, "daily"): return
    user   = await ensure_user(interaction)
    is_vip = db.VIP_ROLE_ID and any(r.id == db.VIP_ROLE_ID for r in interaction.user.roles)

    ok, blood, hex_, streak, next_claim = db.claim_daily(str(interaction.user.id), is_vip)
    if not ok:
        ts = int(next_claim.timestamp())
        await interaction.response.send_message(
            embed=em.error_embed(f"Already performed today. Come back at <t:{ts}:t> (<t:{ts}:R>). 😈"),
            ephemeral=True
        )
        return
    user     = db.get_user(str(interaction.user.id))
    new_tier = db.check_and_grant_tier_reward(str(interaction.user.id))
    await interaction.response.send_message(
        embed=em.daily_claimed_embed(blood, hex_, streak, user["blood"], user["hex"], is_vip),
        ephemeral=True
    )
    if new_tier:
        await interaction.followup.send(embed=em.tier_up_embed(new_tier, user), ephemeral=True)
        await _sync_tier_role(interaction.guild, interaction.user, new_tier)


@tree.command(name="balance", description="Check your blood, hex, and void 🩸")
async def cmd_balance(interaction: discord.Interaction):
    if await check_banned(interaction, "balance"): return
    user = await ensure_user(interaction)
    tx   = db.get_transaction_history(str(interaction.user.id), 10)
    await interaction.response.send_message(embed=em.balance_embed(user, tx), ephemeral=True)


@tree.command(name="earn", description="See all ways to earn currency in the Den 😈")
async def cmd_earn(interaction: discord.Interaction):
    if await check_banned(interaction, "earn"): return
    user          = await ensure_user(interaction)
    completed     = db.get_completed_tasks(str(interaction.user.id))
    today         = __import__("datetime").date.today().isoformat()
    u             = db.get_user(str(interaction.user.id))
    daily_claimed = u["last_daily"] == today if u else False
    is_vip        = db.VIP_ROLE_ID and any(r.id == db.VIP_ROLE_ID for r in interaction.user.roles)
    await interaction.response.send_message(
        embed=em.earn_overview_embed(user, completed, daily_claimed, is_vip),
        ephemeral=True
    )


# ── /task ─────────────────────────────────────────────────────────────────────

task_group = app_commands.Group(name="task", description="Task commands")
tree.add_command(task_group)


@task_group.command(name="complete", description="Complete a task to earn currency")
@app_commands.describe(task_id="Task to complete")
@app_commands.choices(task_id=[
    app_commands.Choice(name=f"{t['emoji']} {t['label']}", value=t["id"])
    for t in db.TASKS
])
async def cmd_task_complete(interaction: discord.Interaction, task_id: str):
    if await check_banned(interaction, "task"): return
    await ensure_user(interaction)
    ok, reward, err, currency = db.complete_task(str(interaction.user.id), task_id)
    if not ok:
        await interaction.response.send_message(embed=em.error_embed(err), ephemeral=True)
        return
    ce   = _cur(currency)
    user = db.get_user(str(interaction.user.id))
    new_tier = db.check_and_grant_tier_reward(str(interaction.user.id))
    await interaction.response.send_message(
        embed=em.success_embed(f"Task complete. **+{reward} {ce} {currency}** claimed.\nBalance: {ce} {user[currency]}"),
        ephemeral=True
    )
    if new_tier:
        await interaction.followup.send(embed=em.tier_up_embed(new_tier, user), ephemeral=True)
        await _sync_tier_role(interaction.guild, interaction.user, new_tier)


# ── /shop ─────────────────────────────────────────────────────────────────────

@tree.command(name="shop", description="Browse Asmodeus's market 😈")
async def cmd_shop(interaction: discord.Interaction):
    if await check_banned(interaction, "shop"): return
    user     = await ensure_user(interaction)
    has_nsfw = db.NSFW_ROLE_ID and any(r.id == db.NSFW_ROLE_ID for r in interaction.user.roles)
    items    = db.get_all_items(active_only=True, include_nsfw=has_nsfw)
    await interaction.response.send_message(
        embed=em.shop_overview_embed(items, user), ephemeral=True
    )


# ── /buy ──────────────────────────────────────────────────────────────────────

@tree.command(name="buy", description="Purchase an item from Asmodeus")
@app_commands.describe(item_id="ID of the item")
async def cmd_buy(interaction: discord.Interaction, item_id: int):
    if await check_banned(interaction, "buy"): return
    user = await ensure_user(interaction)
    item = db.get_item(item_id)

    if not item or not item["active"]:
        await interaction.response.send_message(
            embed=em.error_embed("Item not found. Check `/shop`."), ephemeral=True
        )
        return

    if item["nsfw"]:
        has_nsfw = db.NSFW_ROLE_ID and any(r.id == db.NSFW_ROLE_ID for r in interaction.user.roles)
        if not has_nsfw:
            await interaction.response.send_message(embed=em.nsfw_gate_embed(item), ephemeral=True)
            return

    if item["remaining"] <= 0:
        waitlist = db.get_waitlist(item_id)
        on_wl    = any(w["discord_id"] == str(interaction.user.id) for w in waitlist)
        pos      = next((w["position"] for w in waitlist
                         if w["discord_id"] == str(interaction.user.id)), None)
        await interaction.response.send_message(
            embed=em.item_detail_embed(item, waitlist, user, on_wl, pos),
            view=WaitlistJoinView(str(interaction.user.id), item_id) if not on_wl else None,
            ephemeral=True
        )
        return

    currency = "hex" if item["nsfw"] else (item["price_currency"] or "blood")
    if user[currency] < item["price"]:
        if currency == "void":
            await interaction.response.send_message(
                embed=em.error_embed(
                    f"Not enough void. You need **{item['price']}** but have **{user['void']}**.\n"
                    "Use `/summon` to obtain void. 🖤"
                ),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=em.error_embed(
                    f"Not enough {currency}. You need **{item['price']}** but have **{user[currency]}**.\n"
                    "Use `/earn` to get more."
                ),
                ephemeral=True
            )
        return

    await interaction.response.send_message(
        embed=em.purchase_confirm_embed(item, user),
        view=PurchaseView(str(interaction.user.id), item_id),
        ephemeral=True
    )


# ── /summon ───────────────────────────────────────────────────────────────────

@tree.command(name="summon", description="Sacrifice blood + hex to summon void 🖤")
async def cmd_summon(interaction: discord.Interaction):
    if await check_banned(interaction, "summon"): return
    user = await ensure_user(interaction)
    await interaction.response.send_message(
        embed=em.summon_confirm_embed(user),
        view=SummonView(str(interaction.user.id)),
        ephemeral=True
    )


# ── /rank ─────────────────────────────────────────────────────────────────────

@tree.command(name="rank", description="Check your standing in the Den 😈")
async def cmd_rank(interaction: discord.Interaction):
    if await check_banned(interaction, "rank"): return
    await ensure_user(interaction)
    rank = db.get_user_rank(str(interaction.user.id))
    if not rank:
        await interaction.response.send_message(
            embed=em.error_embed("No rank yet. Start earning."), ephemeral=True
        )
        return
    await interaction.response.send_message(embed=em.rank_embed(rank), ephemeral=True)


# ── /leaderboard ──────────────────────────────────────────────────────────────

@tree.command(name="leaderboard", description="The most damned souls in the Den 🔥")
async def cmd_leaderboard(interaction: discord.Interaction):
    if await check_banned(interaction, "leaderboard"): return
    await ensure_user(interaction)
    entries = db.get_leaderboard()
    db.save_leaderboard_snapshot(entries)
    await interaction.response.send_message(
        embed=em.leaderboard_embed(entries, str(interaction.user.id)),
        ephemeral=False
    )
    guild = interaction.guild
    if guild:
        for entry in entries:
            member = guild.get_member(int(entry["discord_id"]))
            if member:
                await _sync_tier_role(guild, member, entry["tier"])


# ── /purchases ────────────────────────────────────────────────────────────────

@tree.command(name="purchases", description="Your offering history")
async def cmd_purchases(interaction: discord.Interaction):
    if await check_banned(interaction, "purchases"): return
    await ensure_user(interaction)
    purchases = db.get_user_purchases(str(interaction.user.id))
    if not purchases:
        await interaction.response.send_message(
            embed=em.error_embed("No offerings yet. Check `/shop`."), ephemeral=True
        )
        return
    lines = []
    for p in purchases[:15]:
        cur   = p["price_currency"] or "blood"
        badge = "⚙️" if p["fulfillment"] == "auto" else "📋"
        lines.append(
            f"{badge} **{p['item_name']}** — {_cur(cur)} {p['price']} {cur} · {p['bought_at'][:10]}"
        )
    e = discord.Embed(title="😈 Your Offerings", description="\n".join(lines), color=em.COLOR_BLOOD)
    e.set_footer(text="⚙️ auto-fulfilled  ·  📋 manual (awaiting delivery)")
    await interaction.response.send_message(embed=e, ephemeral=True)


# ── /gift ─────────────────────────────────────────────────────────────────────

@tree.command(name="gift", description="Send blood or hex to another soul")
@app_commands.describe(user="Who to gift", amount="Amount",
                       currency="Currency (void cannot be gifted)")
@app_commands.choices(currency=CURRENCY_CHOICES)
async def cmd_gift(interaction: discord.Interaction,
                   user: discord.Member, amount: int,
                   currency: str = "blood"):
    if await check_banned(interaction, "gift"): return
    if user.id == interaction.user.id:
        await interaction.response.send_message(
            embed=em.error_embed("You can't gift yourself."), ephemeral=True
        )
        return
    await ensure_user(interaction)
    db.get_or_create_user(str(user.id), user.display_name)
    ok, msg = db.send_member_gift(str(interaction.user.id), str(user.id), amount, currency)
    if not ok:
        await interaction.response.send_message(embed=em.error_embed(msg), ephemeral=True)
        return
    ce = _cur(currency)
    await interaction.response.send_message(
        embed=em.success_embed(f"Sent **{amount} {ce} {currency}** to {user.mention}. 🔥"),
        ephemeral=True
    )
    try:
        await user.send(embed=discord.Embed(
            title="🩸 You received a gift",
            description=f"{interaction.user.display_name} sent you **{amount} {ce} {currency}** in Devil's Den.",
            color=em.COLOR_GIFT
        ))
    except Exception:
        pass


# ── /gifts ────────────────────────────────────────────────────────────────────

gifts_group = app_commands.Group(name="gifts", description="Gift commands")
tree.add_command(gifts_group)


@gifts_group.command(name="view", description="See unclaimed gifts")
async def cmd_gifts_view(interaction: discord.Interaction):
    if await check_banned(interaction, "gifts"): return
    await ensure_user(interaction)
    gifts = db.get_unclaimed_gifts(str(interaction.user.id))
    await interaction.response.send_message(embed=em.gifts_embed(gifts), ephemeral=True)


@gifts_group.command(name="claim", description="Claim a gift by ID")
@app_commands.describe(gift_id="Gift ID")
async def cmd_gifts_claim(interaction: discord.Interaction, gift_id: int):
    if await check_banned(interaction, "gifts"): return
    await ensure_user(interaction)
    ok, amount, message, currency = db.claim_gift(str(interaction.user.id), gift_id)
    if not ok:
        await interaction.response.send_message(embed=em.error_embed(message), ephemeral=True)
        return
    ce = _cur(currency)
    await interaction.response.send_message(
        embed=em.success_embed(
            f"Claimed **{amount} {ce} {currency}**."
            + (f'\n*"{message}"*' if message else "")
        ),
        ephemeral=True
    )


@gifts_group.command(name="claimall", description="Claim all pending gifts")
async def cmd_gifts_claimall(interaction: discord.Interaction):
    if await check_banned(interaction, "gifts"): return
    await ensure_user(interaction)
    count, totals = db.claim_all_gifts(str(interaction.user.id))
    if count == 0:
        await interaction.response.send_message(
            embed=em.error_embed("Nothing to claim."), ephemeral=True
        )
        return
    total_str = "  ·  ".join(f"**{v} {_cur(k)} {k}**" for k, v in totals.items())
    await interaction.response.send_message(
        embed=em.success_embed(f"Claimed **{count} gift{'s' if count > 1 else ''}**.\n{total_str}"),
        ephemeral=True
    )


# ── /waitlist ─────────────────────────────────────────────────────────────────

waitlist_group = app_commands.Group(name="waitlist", description="Waitlist commands")
tree.add_command(waitlist_group)


@waitlist_group.command(name="join", description="Mark your place for a sold-out item")
@app_commands.describe(item_id="Item ID")
async def cmd_waitlist_join(interaction: discord.Interaction, item_id: int):
    if await check_banned(interaction, "waitlist"): return
    await ensure_user(interaction)
    item = db.get_item(item_id)
    if not item:
        await interaction.response.send_message(embed=em.error_embed("Item not found."), ephemeral=True)
        return
    ok, msg, position = db.join_waitlist(str(interaction.user.id), item_id)
    if not ok:
        await interaction.response.send_message(embed=em.error_embed(msg), ephemeral=True)
        return
    await interaction.response.send_message(
        embed=em.success_embed(
            f"Your place for **{item['emoji']} {item['name']}** is marked.\n"
            f"You're **#{position}** in line. 🩸"
        ),
        ephemeral=True
    )


@waitlist_group.command(name="leave", description="Leave a waitlist and reclaim your blood")
@app_commands.describe(item_id="Item ID")
async def cmd_waitlist_leave(interaction: discord.Interaction, item_id: int):
    if await check_banned(interaction, "waitlist"): return
    await ensure_user(interaction)
    ok, msg = db.leave_waitlist(str(interaction.user.id), item_id)
    if not ok:
        await interaction.response.send_message(embed=em.error_embed(msg), ephemeral=True)
        return
    await interaction.response.send_message(embed=em.success_embed(msg), ephemeral=True)


@waitlist_group.command(name="view", description="See all your waitlist positions")
async def cmd_waitlist_view(interaction: discord.Interaction):
    if await check_banned(interaction, "waitlist"): return
    await ensure_user(interaction)
    wl = db.get_user_waitlists(str(interaction.user.id))
    await interaction.response.send_message(embed=em.waitlist_my_embed(wl), ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
#  ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

admin_group = app_commands.Group(name="admin", description="Admin commands")
tree.add_command(admin_group)


@admin_group.command(name="stats", description="Full Den overview")
async def cmd_admin_stats(interaction: discord.Interaction):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    stats = db.get_shop_stats()
    await interaction.response.send_message(embed=em.admin_stats_embed(stats), ephemeral=True)


@admin_group.command(name="addbalance", description="Add currency to a soul's wallet")
@app_commands.describe(user="User", amount="Amount", currency="Currency")
@app_commands.choices(currency=ALL_CURRENCY_CHOICES)
async def cmd_admin_addbalance(interaction: discord.Interaction,
                                user: discord.Member, amount: int,
                                currency: str = "blood"):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    db.get_or_create_user(str(user.id), user.display_name)
    db.update_currency(str(user.id), currency, amount, "Admin credit", "admin_grant")
    await interaction.response.send_message(
        embed=em.success_embed(f"Added **{amount} {_cur(currency)} {currency}** to {user.mention}."),
        ephemeral=True
    )


@admin_group.command(name="gift", description="Send a gift to a soul (from Asmodeus)")
@app_commands.describe(user="Who", amount="Amount", currency="Currency", message="Message")
@app_commands.choices(currency=ALL_CURRENCY_CHOICES)
async def cmd_admin_gift(interaction: discord.Interaction,
                          user: discord.Member, amount: int,
                          currency: str = "blood", message: str = ""):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    db.get_or_create_user(str(user.id), user.display_name)
    db.send_gift(str(user.id), amount, message, currency)
    ce = _cur(currency)
    await interaction.response.send_message(
        embed=em.success_embed(
            f"Gift of **{amount} {ce} {currency}** queued for {user.mention}.\n"
            "They can claim it with `/gifts view`."
        ),
        ephemeral=True
    )
    try:
        await user.send(embed=discord.Embed(
            title="🩸 Asmodeus left you something.",
            description=(
                f"**{amount} {ce} {currency}** is waiting for you.\n"
                + (f'*"{message}"*\n' if message else "")
                + "\nUse `/gifts claim` to collect it. 😈"
            ),
            color=em.COLOR_GIFT
        ))
    except Exception:
        pass


@admin_group.command(name="additem", description="Add an item to Asmodeus's market")
@app_commands.describe(
    name="Item name", description="Description", price="Price",
    quantity="Quantity", category="Category", emoji="Emoji", tag="Short tag",
    currency="Currency (NSFW items always cost hex)",
    nsfw="Restrict to NSFW role?",
    fulfillment="auto = instant, manual = requires delivery"
)
@app_commands.choices(currency=ALL_CURRENCY_CHOICES)
@app_commands.choices(fulfillment=[
    app_commands.Choice(name="auto — instant, no approval needed", value="auto"),
    app_commands.Choice(name="manual — posts to requests channel", value="manual"),
])
async def cmd_admin_additem(interaction: discord.Interaction,
                             name: str, description: str, price: int,
                             quantity: int, category: str, emoji: str, tag: str,
                             currency: str = "blood", nsfw: bool = False,
                             fulfillment: str = "auto"):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    item_id         = db.admin_add_item(name, description, price, quantity,
                                         category, emoji, tag, nsfw, currency, fulfillment)
    actual_currency = "hex" if nsfw else currency
    ce              = _cur(actual_currency)
    await interaction.response.send_message(
        embed=em.success_embed(
            f"Added **{emoji} {name}** (ID: `{item_id}`)\n"
            f"Price: {ce} {price} {actual_currency}  ·  "
            f"Fulfillment: **{fulfillment}**"
            + (" · 🔞 NSFW" if nsfw else "")
        ),
        ephemeral=True
    )


@admin_group.command(name="toggleitem", description="Show or hide an item")
@app_commands.describe(item_id="Item ID")
async def cmd_admin_toggleitem(interaction: discord.Interaction, item_id: int):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    ok, msg = db.admin_toggle_item(item_id)
    if not ok:
        await interaction.response.send_message(embed=em.error_embed(msg), ephemeral=True)
        return
    await interaction.response.send_message(embed=em.success_embed(msg), ephemeral=True)


@admin_group.command(name="togglefulfillment", description="Flip an item between auto and manual fulfillment")
@app_commands.describe(item_id="Item ID")
async def cmd_admin_togglefulfillment(interaction: discord.Interaction, item_id: int):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    ok, msg = db.toggle_fulfillment(item_id)
    if not ok:
        await interaction.response.send_message(embed=em.error_embed(msg), ephemeral=True)
        return
    await interaction.response.send_message(embed=em.success_embed(msg), ephemeral=True)

    item = db.get_item(item_id)
    if item and FULFILLMENT_LOG_CH:
        log_ch = interaction.guild.get_channel(FULFILLMENT_LOG_CH)
        if log_ch:
            mode = item["fulfillment"]
            color = em.COLOR_DANGER if mode == "manual" else 0x2a4a1a
            icon  = "📋" if mode == "manual" else "⚙️"
            try:
                await log_ch.send(embed=discord.Embed(
                    title=f"{icon} Fulfillment Mode Changed",
                    description=(
                        f"**{item['emoji']} {item['name']}** is now set to **{mode}** fulfillment.\n"
                        + ("Orders will be posted to the requests channel for manual delivery. 😈"
                           if mode == "manual"
                           else "Orders will be fulfilled instantly and automatically. ⚙️")
                    ),
                    color=color
                ).set_footer(text=f"Changed by {interaction.user.display_name}"))
            except Exception:
                pass



@admin_group.command(name="edititem", description="Edit an existing item's fields")
@app_commands.describe(
    item_id="Item ID to edit",
    name="New name",
    description="New description",
    price="New price",
    emoji="New emoji",
    category="New category",
    tag="New short tag",
    currency="New currency",
    fulfillment="New fulfillment mode",
    nsfw="Change NSFW status",
)
@app_commands.choices(currency=ALL_CURRENCY_CHOICES)
@app_commands.choices(fulfillment=[
    app_commands.Choice(name="auto — instant, no approval needed", value="auto"),
    app_commands.Choice(name="manual — posts to requests channel", value="manual"),
])
async def cmd_admin_edititem(interaction: discord.Interaction, item_id: int,
                              name: str = None, description: str = None,
                              price: int = None, emoji: str = None,
                              category: str = None, tag: str = None,
                              currency: str = None, fulfillment: str = None,
                              nsfw: bool = None):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    ok, msg = db.admin_edit_item(
        item_id,
        name=name, description=description, price=price,
        emoji=emoji, category=category, tag=tag,
        price_currency=currency, fulfillment=fulfillment,
        nsfw=(1 if nsfw else 0) if nsfw is not None else None,
    )
    if not ok:
        await interaction.response.send_message(embed=em.error_embed(msg), ephemeral=True)
        return
    await interaction.response.send_message(embed=em.success_embed(msg), ephemeral=True)


@admin_group.command(name="balance", description="Check any soul's wallet")
@app_commands.describe(user="User to inspect")
async def cmd_admin_balance(interaction: discord.Interaction, user: discord.Member):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    db.get_or_create_user(str(user.id), user.display_name)
    target = db.get_user(str(user.id))
    tx     = db.get_transaction_history(str(user.id), 10)
    embed  = em.balance_embed(target, tx)
    embed.title = f"😈 {user.display_name}'s Wallet"
    await interaction.response.send_message(embed=embed, ephemeral=True)


@admin_group.command(name="award", description="Give currency to every member with a specific role")
@app_commands.describe(
    role="Role to award",
    amount="Amount to give each member",
    currency="Currency to award",
    reason="Reason shown in transaction history (optional)"
)
@app_commands.choices(currency=ALL_CURRENCY_CHOICES)
async def cmd_admin_award(interaction: discord.Interaction,
                           role: discord.Role, amount: int,
                           currency: str = "blood", reason: str = "Role award"):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message(
            embed=em.error_embed("Amount must be greater than 0."), ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    members = [m for m in role.members if not m.bot]
    if not members:
        await interaction.followup.send(
            embed=em.error_embed(f"No members found with {role.mention}."), ephemeral=True
        )
        return

    ce = _cur(currency)
    for member in members:
        db.get_or_create_user(str(member.id), member.display_name)
        db.update_currency(str(member.id), currency, amount, reason, "admin_grant")

    await interaction.followup.send(
        embed=em.success_embed(
            f"Awarded **{amount} {ce} {currency}** to **{len(members)} member{'s' if len(members) != 1 else ''}** with {role.mention}.\n"
            f"*Reason: {reason}*"
        ),
        ephemeral=True
    )


@admin_group.command(name="restock", description="Restock a sold-out item")
@app_commands.describe(item_id="Item ID", quantity="New quantity")
async def cmd_admin_restock(interaction: discord.Interaction, item_id: int, quantity: int):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    ok, msg = db.admin_restock(item_id, quantity)
    if not ok:
        await interaction.response.send_message(embed=em.error_embed(msg), ephemeral=True)
        return
    await interaction.response.send_message(embed=em.success_embed(msg), ephemeral=True)

    waitlist = db.get_waitlist(item_id)
    item     = db.get_item(item_id)
    for entry in waitlist:
        try:
            member = interaction.guild.get_member(int(entry["discord_id"]))
            if member:
                await member.send(embed=discord.Embed(
                    title=f"🔥 {item['emoji']} {item['name']} is back.",
                    description=f"You're **#{entry['position']}** on the waitlist.\nUse `/buy {item_id}` now. 😈",
                    color=em.COLOR_BLOOD
                ))
        except Exception:
            pass


@admin_group.command(name="givevoid", description="Grant 1 void to a soul (1x per month)")
@app_commands.describe(user="User to grant void to")
async def cmd_admin_givevoid(interaction: discord.Interaction, user: discord.Member):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    db.get_or_create_user(str(user.id), user.display_name)
    ok, msg = db.admin_grant_void(str(interaction.user.id), str(user.id))
    if not ok:
        await interaction.response.send_message(embed=em.error_embed(msg), ephemeral=True)
        return
    await interaction.response.send_message(embed=em.success_embed(msg), ephemeral=True)
    try:
        await user.send(embed=discord.Embed(
            title="🖤 Asmodeus granted you void.",
            description="**1 void** has been placed in your wallet.\nYou are among the truly damned. 😈",
            color=em.COLOR_VOID_V
        ))
    except Exception:
        pass


@admin_group.command(name="voidstatus", description="Check your monthly void grant status")
async def cmd_admin_voidstatus(interaction: discord.Interaction):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    used, msg = db.admin_void_grant_status(str(interaction.user.id))
    await interaction.response.send_message(
        embed=em.success_embed(msg) if used else em.error_embed(msg), ephemeral=True
    )


@admin_group.command(name="giftlimit", description="Set the global daily gift limit")
@app_commands.describe(limit="Max any soul can gift per day")
async def cmd_admin_giftlimit(interaction: discord.Interaction, limit: int):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    db.set_global_gift_limit(limit)
    await interaction.response.send_message(
        embed=em.success_embed(f"Global gift limit set to **{limit}/day**."), ephemeral=True
    )


@admin_group.command(name="setgiftlimit", description="Override gift limit for a specific user")
@app_commands.describe(user="User", limit="Their daily gift limit")
async def cmd_admin_setgiftlimit(interaction: discord.Interaction,
                                  user: discord.Member, limit: int):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    ok, msg = db.set_gift_limit(str(user.id), limit)
    if not ok:
        await interaction.response.send_message(embed=em.error_embed(msg), ephemeral=True)
        return
    await interaction.response.send_message(embed=em.success_embed(msg), ephemeral=True)


@admin_group.command(name="dropadd", description="Schedule a drop")
@app_commands.describe(item_id="Item", quantity="Units to add", minutes="Minutes from now")
async def cmd_admin_dropadd(interaction: discord.Interaction,
                              item_id: int, quantity: int, minutes: int):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    if not DROPS_CH:
        await interaction.response.send_message(
            embed=em.error_embed("Drops channel not set. Fill in DROPS_CH in bot.py first."),
            ephemeral=True
        )
        return
    item = db.get_item(item_id)
    if not item:
        await interaction.response.send_message(embed=em.error_embed("Item not found."), ephemeral=True)
        return

    drop_at = datetime.now(EST) + timedelta(minutes=minutes)
    drop_id = db.schedule_drop(item_id, quantity, drop_at.isoformat(), str(DROPS_CH))

    drops_ch = await get_drops_channel(interaction.guild)
    if drops_ch:
        drop = db.get_drop(drop_id)
        sent = await drops_ch.send(embed=em.drop_countdown_embed(drop, minutes * 60))
        db.set_drop_message_id(drop_id, str(sent.id))

    await interaction.response.send_message(
        embed=em.success_embed(
            f"Drop scheduled. **{item['emoji']} {item['name']}** drops in **{minutes} minutes**.\n"
            f"Drop ID: `{drop_id}`"
        ),
        ephemeral=True
    )


@admin_group.command(name="droplist", description="List all scheduled drops")
async def cmd_admin_droplist(interaction: discord.Interaction):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    drops = db.get_active_drops()
    if not drops:
        await interaction.response.send_message(
            embed=em.error_embed("No active drops."), ephemeral=True
        )
        return
    lines = [
        f"**`#{d['id']}`** {d['emoji']} **{d['item_name']}** — "
        f"{d['restock_qty']} units · `{d['drop_at'][:16]}` UTC · *{d['status']}*"
        for d in drops
    ]
    await interaction.response.send_message(
        embed=discord.Embed(title="⏳ Scheduled Drops",
                            description="\n".join(lines), color=em.COLOR_BLOOD),
        ephemeral=True
    )


@admin_group.command(name="dropcancel", description="Cancel a drop")
@app_commands.describe(drop_id="Drop ID")
async def cmd_admin_dropcancel(interaction: discord.Interaction, drop_id: int):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 😈", ephemeral=True)
        return
    ok, msg = db.cancel_drop(drop_id)
    if not ok:
        await interaction.response.send_message(embed=em.error_embed(msg), ephemeral=True)
        return
    drop     = db.get_drop(drop_id)
    drops_ch = await get_drops_channel(interaction.guild)
    if drops_ch and drop and drop["message_id"]:
        try:
            old_msg = await drops_ch.fetch_message(int(drop["message_id"]))
            await old_msg.edit(embed=discord.Embed(
                title="❌ Drop Cancelled",
                description=f"The drop for **{drop['emoji']} {drop['item_name']}** has been cancelled.",
                color=em.COLOR_DANGER
            ))
        except Exception:
            pass
    await interaction.response.send_message(embed=em.success_embed(msg), ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
#  TIER ROLE SYNC
# ─────────────────────────────────────────────────────────────────────────────

TIER_COLORS = {
    "Damned Elite":   discord.Color.dark_red(),
    "Hell's Devoted": discord.Color.red(),
    "Blood Pact":     discord.Color.from_rgb(100, 0, 0),
    "Corrupted":      discord.Color.dark_magenta(),
    "Fresh Meat":     discord.Color.dark_gray(),
}


async def _sync_tier_role(guild, member, tier):
    if not guild or not member:
        return
    tier_names = [t["name"] for t in db.TIERS]
    for name in tier_names:
        existing = discord.utils.get(guild.roles, name=name)
        if not existing:
            try:
                existing = await guild.create_role(
                    name=name, color=TIER_COLORS.get(name, discord.Color.default())
                )
            except Exception:
                continue
        if name == tier["name"]:
            if existing not in member.roles:
                try: await member.add_roles(existing)
                except Exception: pass
        else:
            if existing in member.roles:
                try: await member.remove_roles(existing)
                except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
#  DROP TICKER
# ─────────────────────────────────────────────────────────────────────────────

@tasks.loop(seconds=60)
async def drop_ticker():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    drops_ch = guild.get_channel(DROPS_CH) if DROPS_CH else None
    if not drops_ch:
        return

    now   = datetime.now(EST)
    drops = db.get_active_drops()

    for drop in drops:
        try:
            drop_time    = datetime.fromisoformat(drop["drop_at"])
            if drop_time.tzinfo is None:
                drop_time = drop_time.replace(tzinfo=EST)
            seconds_left = int((drop_time - now).total_seconds())

            if drop["message_id"]:
                try:
                    msg = await drops_ch.fetch_message(int(drop["message_id"]))
                    await msg.edit(embed=em.drop_countdown_embed(drop, max(0, seconds_left)))
                except Exception:
                    pass

            if seconds_left <= 0 and drop["status"] == "live":
                db.admin_restock(drop["item_id"], drop["restock_qty"])
                db.complete_drop(drop["id"])
                for entry in db.get_waitlist(drop["item_id"]):
                    try:
                        member = guild.get_member(int(entry["discord_id"]))
                        if member:
                            await member.send(embed=discord.Embed(
                                title=f"🔥 {drop['emoji']} {drop['item_name']} just dropped.",
                                description=f"You're **#{entry['position']}** on the waitlist.\nUse `/buy {drop['item_id']}` now. 😈",
                                color=em.COLOR_BLOOD
                            ))
                    except Exception:
                        pass
        except Exception as ex:
            print(f"Drop ticker error for drop {drop['id']}: {ex}")



@tasks.loop(hours=1)
async def order_expiry_ticker():
    """Check every hour for pending orders older than 3 days and auto-close them."""
    expired = db.get_expired_orders()
    if not expired:
        return

    guild = bot.get_guild(GUILD_ID)

    for order in expired:
        closed = db.auto_close_order(order["id"])
        if not closed:
            continue

        currency = closed["price_currency"]
        price    = closed["price"]

        # Refund the buyer
        db.update_currency(
            closed["discord_id"], currency, price,
            f"Auto-refund — order #{closed['id']} expired after 3 days", "refund"
        )

        # DM the buyer
        if guild:
            try:
                member = guild.get_member(int(closed["discord_id"]))
                if member:
                    await member.send(embed=discord.Embed(
                        title="↩️ Order Expired — Refund Issued",
                        description=(
                            f"Your order for **{closed['item_name']}** was not fulfilled within 3 days "
                            f"and has been automatically closed.\n\n"
                            f"**{price} {currency}** has been returned to your wallet. 🖤"
                        ),
                        color=em.COLOR_DANGER
                    ))
            except Exception:
                pass

        # Edit the original order card in requests channel to show expired
        if guild and closed.get("message_id") and REQUESTS_CH:
            try:
                requests_ch = guild.get_channel(REQUESTS_CH)
                if requests_ch:
                    msg = await requests_ch.fetch_message(int(closed["message_id"]))
                    await msg.edit(
                        embed=discord.Embed(
                            title=f"⏰ Expired — {closed['item_name']}",
                            description=f"Order #{closed['id']} auto-closed after 3 days. Buyer refunded.",
                            color=em.COLOR_DANGER
                        ),
                        view=None
                    )
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
#  BAN COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

BANNABLE_COMMANDS = ["daily","balance","earn","shop","buy","summon","task",
                     "purchases","gift","gifts","waitlist","rank","leaderboard"]

@admin_group.command(name="ban", description="Ban a soul from the bot or a specific command")
@app_commands.describe(
    user="Soul to ban",
    command="Leave blank for a full bot ban, or specify a command name",
    reason="Reason for the ban (optional)"
)
@app_commands.choices(command=[
    app_commands.Choice(name=c, value=c) for c in BANNABLE_COMMANDS
])
async def cmd_admin_ban(interaction: discord.Interaction,
                         user: discord.Member,
                         command: str = None,
                         reason: str = None):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 🖤", ephemeral=True)
        return
    if is_admin(user.id):
        await interaction.response.send_message(
            embed=em.error_embed("You cannot ban an admin."), ephemeral=True
        )
        return
    ok, msg = db.ban_user(str(user.id), str(interaction.user.id), command, reason)
    label = f"`/{command}`" if command else "the entire bot"
    if ok:
        await interaction.response.send_message(
            embed=em.success_embed(f"**{user.display_name}** has been banned from {label}."),
            ephemeral=True
        )
    else:
        await interaction.response.send_message(embed=em.error_embed(msg), ephemeral=True)


@admin_group.command(name="unban", description="Lift a ban from a soul")
@app_commands.describe(
    user="Soul to unban",
    command="Leave blank to lift a full ban, or specify the command ban to remove"
)
@app_commands.choices(command=[
    app_commands.Choice(name=c, value=c) for c in BANNABLE_COMMANDS
])
async def cmd_admin_unban(interaction: discord.Interaction,
                           user: discord.Member,
                           command: str = None):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 🖤", ephemeral=True)
        return
    ok, msg = db.unban_user(str(user.id), command)
    if ok:
        label = f"`/{command}`" if command else "the bot"
        await interaction.response.send_message(
            embed=em.success_embed(f"**{user.display_name}** has been unbanned from {label}."),
            ephemeral=True
        )
    else:
        await interaction.response.send_message(embed=em.error_embed(msg), ephemeral=True)


@admin_group.command(name="banlist", description="View all active bans")
async def cmd_admin_banlist(interaction: discord.Interaction):
    if not require_admin(interaction):
        await interaction.response.send_message("Admins only. 🖤", ephemeral=True)
        return

    bans = db.get_ban_list()
    if not bans:
        await interaction.response.send_message(
            embed=em.success_embed("No active bans. The Den is clean."), ephemeral=True
        )
        return

    full_bans = [b for b in bans if b["command"] is None]
    cmd_bans  = [b for b in bans if b["command"] is not None]
    lines     = []

    if full_bans:
        lines.append("**🖤 Full Bot Bans**")
        for b in full_bans:
            member = interaction.guild.get_member(int(b["discord_id"]))
            name   = member.display_name if member else f"`{b['discord_id']}`"
            reason = f" — *{b['reason']}*" if b["reason"] else ""
            lines.append(f"  · {name}{reason}")

    if cmd_bans:
        if lines:
            lines.append("")
        lines.append("**⛔ Command Bans**")
        for b in cmd_bans:
            member = interaction.guild.get_member(int(b["discord_id"]))
            name   = member.display_name if member else f"`{b['discord_id']}`"
            reason = f" — *{b['reason']}*" if b["reason"] else ""
            lines.append(f"  · {name} · `/{b['command']}`{reason}")

    e = discord.Embed(
        title="🖤 Active Bans",
        description="\n".join(lines),
        color=em.COLOR_DANGER
    )
    e.set_footer(text=f"{len(bans)} total ban{'s' if len(bans) != 1 else ''}")
    await interaction.response.send_message(embed=e, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────────────────────────────────────

bot.run(TOKEN)