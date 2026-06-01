import discord
from discord import app_commands
from discord.ext import commands, tasks
import chat_exporter
import io
import aiosqlite
import datetime
import asyncio
import aiohttp
import os

# ==========================================
# ⚙️ الإعدادات الأساسية
# ==========================================
LOGO_URL       = "https://gcdnb.pbrd.co/images/0N0gWj06hL8e.png?o=1"
GUILD_ID       = 1342214823844249650
LOG_CHANNEL_ID = 1351642089489961011
STAFF_ROLE_ID  = 1351641506426916914

CATEGORY_IDS = {
    "مشكلة":   1510472552663879885,
    "استفسار": 1510472580186767521,
    "شراء":    1510472497378885743,
}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

active_tickets: dict[int, dict] = {}
voice_tickets:  dict[int, int]  = {}


# ==========================================
# 🗄️ قاعدة البيانات
# ==========================================
async def setup_db():
    async with aiosqlite.connect("nova_tickets.db") as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS ticket_count
            (id INTEGER PRIMARY KEY, count INTEGER)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS blacklist
            (user_id INTEGER PRIMARY KEY)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS staff_stats
            (staff_id INTEGER PRIMARY KEY, claimed INTEGER DEFAULT 0, closed INTEGER DEFAULT 0)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS staff_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER, ticket_name TEXT, note TEXT, created_at TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS user_stats
            (user_id INTEGER PRIMARY KEY, total_opened INTEGER DEFAULT 0)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS ticket_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_num INTEGER,
            user_id INTEGER,
            ticket_type TEXT,
            opened_by INTEGER,
            claimed_by INTEGER,
            closed_by INTEGER,
            opened_at TEXT,
            closed_at TEXT,
            response_seconds INTEGER,
            close_reason TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER,
            user_id INTEGER,
            ticket_name TEXT,
            stars INTEGER,
            feedback TEXT,
            created_at TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS user_timeouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            mod_id INTEGER,
            reason TEXT,
            created_at TEXT
        )""")
        # ✅ جدول سجل التحويل
        await db.execute("""CREATE TABLE IF NOT EXISTS transfer_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_name TEXT,
            from_type TEXT,
            to_type TEXT,
            transferred_by INTEGER,
            transferred_at TEXT
        )""")
        async with db.execute("SELECT count FROM ticket_count WHERE id = 1") as cursor:
            if not await cursor.fetchone():
                await db.execute("INSERT INTO ticket_count (id, count) VALUES (1, 0)")
        await db.commit()


async def get_next_ticket_num() -> int:
    async with aiosqlite.connect("nova_tickets.db") as db:
        async with db.execute("SELECT count FROM ticket_count WHERE id = 1") as cursor:
            row   = await cursor.fetchone()
            count = (row[0] if row else 0) + 1
        await db.execute("UPDATE ticket_count SET count = ? WHERE id = 1", (count,))
        await db.commit()
        return count


def is_staff(interaction: discord.Interaction) -> bool:
    return (
        STAFF_ROLE_ID in [r.id for r in interaction.user.roles]
        or interaction.user.guild_permissions.administrator
    )


# ==========================================
# ⭐ نظام التقييم
# ==========================================
class RatingModal(discord.ui.Modal, title="ملاحظات التقييم"):
    feedback = discord.ui.TextInput(
        label="هل لديك ملاحظات إضافية؟",
        style=discord.TextStyle.paragraph,
        placeholder="اكتب ملاحظاتك هنا لتحسين الخدمة...",
        required=False, max_length=500
    )

    def __init__(self, stars: int, ticket_name: str, staff_member, original_view):
        super().__init__()
        self.stars         = stars
        self.ticket_name   = ticket_name
        self.staff_member  = staff_member
        self.original_view = original_view

    async def on_submit(self, interaction: discord.Interaction):
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            embed = discord.Embed(title="⭐ تقييم دعم فني جديد", color=discord.Color.gold())
            embed.add_field(name="التذكرة",         value=self.ticket_name, inline=True)
            embed.add_field(name="الإداري المستلم", value=self.staff_member.mention if self.staff_member else "غير محدد", inline=True)
            embed.add_field(name="التقييم",         value="⭐" * self.stars, inline=False)
            embed.add_field(name="ملاحظات العميل",  value=self.feedback.value or "لا توجد ملاحظات", inline=False)
            embed.add_field(name="تقييم من قِبل",   value=interaction.user.mention, inline=False)
            embed.set_thumbnail(url=LOGO_URL)
            await log_channel.send(embed=embed)

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if self.staff_member:
            async with aiosqlite.connect("nova_tickets.db") as db:
                await db.execute(
                    """INSERT INTO ratings (staff_id, user_id, ticket_name, stars, feedback, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (self.staff_member.id, interaction.user.id,
                     self.ticket_name, self.stars,
                     self.feedback.value or "", now_str)
                )
                await db.commit()
        for child in self.original_view.children:
            child.disabled = True
        await interaction.response.edit_message(view=self.original_view)
        await interaction.followup.send("✅ شكراً لتقييمك!", ephemeral=True)


class NoteModal(discord.ui.Modal, title="📝 ملاحظة داخلية سرية"):
    note_text = discord.ui.TextInput(
        label="الملاحظة (لن يراها العميل أبداً)",
        style=discord.TextStyle.paragraph,
        placeholder="اكتب ملاحظتك السرية هنا...",
        required=True, max_length=1000
    )

    def __init__(self, ticket_name: str):
        super().__init__()
        self.ticket_name = ticket_name

    async def on_submit(self, interaction: discord.Interaction):
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
        async with aiosqlite.connect("nova_tickets.db") as db:
            await db.execute(
                "INSERT INTO staff_notes (staff_id, ticket_name, note, created_at) VALUES (?, ?, ?, ?)",
                (interaction.user.id, self.ticket_name, self.note_text.value, now)
            )
            await db.commit()
        await interaction.response.send_message("✅ تم حفظ الملاحظة السرية.", ephemeral=True)


class RatingView(discord.ui.View):
    def __init__(self, ticket_name: str, staff_member):
        super().__init__(timeout=86400)
        self.ticket_name  = ticket_name
        self.staff_member = staff_member

    async def _handle_rating(self, interaction: discord.Interaction, stars: int):
        await interaction.response.send_modal(
            RatingModal(stars, self.ticket_name, self.staff_member, self)
        )

    @discord.ui.button(label="1", emoji="⭐", style=discord.ButtonStyle.secondary)
    async def star_1(self, i, b): await self._handle_rating(i, 1)
    @discord.ui.button(label="2", emoji="⭐", style=discord.ButtonStyle.secondary)
    async def star_2(self, i, b): await self._handle_rating(i, 2)
    @discord.ui.button(label="3", emoji="⭐", style=discord.ButtonStyle.secondary)
    async def star_3(self, i, b): await self._handle_rating(i, 3)
    @discord.ui.button(label="4", emoji="⭐", style=discord.ButtonStyle.secondary)
    async def star_4(self, i, b): await self._handle_rating(i, 4)
    @discord.ui.button(label="5", emoji="⭐", style=discord.ButtonStyle.success)
    async def star_5(self, i, b): await self._handle_rating(i, 5)


# ==========================================
# 🔒 Modal إغلاق مع سبب
# ==========================================
class CloseReasonModal(discord.ui.Modal, title="📝 سبب الإغلاق"):
    reason = discord.ui.TextInput(
        label="سبب الإغلاق",
        style=discord.TextStyle.paragraph,
        placeholder="اكتب سبب الإغلاق...",
        required=True, max_length=500
    )

    def __init__(self, controls):
        super().__init__()
        self.controls = controls

    async def on_submit(self, interaction: discord.Interaction):
        await do_close_ticket(interaction, self.controls, close_reason=self.reason.value)


# ==========================================
# 🔒 View اختيار نوع الإغلاق
# ==========================================
class CloseTypeView(discord.ui.View):
    def __init__(self, controls):
        super().__init__(timeout=60)
        self.controls = controls

    @discord.ui.button(label="📁 إغلاق مع حفظ السجل", style=discord.ButtonStyle.danger)
    async def close_with_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        await do_close_ticket(interaction, self.controls)

    @discord.ui.button(label="📝 إغلاق مع سبب", style=discord.ButtonStyle.secondary)
    async def close_with_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CloseReasonModal(self.controls))


# ==========================================
# 🔀 تحويل التذكرة
# ==========================================
class TransferTypeSelect(discord.ui.Select):
    def __init__(self, controls, current_type: str):
        self.controls     = controls
        self.current_type = current_type
        options = [
            discord.SelectOption(label=t, emoji=e, value=t)
            for t, e in [("مشكلة", "🛠️"), ("استفسار", "❓"), ("شراء", "💳")]
            if t != current_type
        ]
        super().__init__(
            placeholder="اختر القسم الجديد...",
            min_values=1, max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        new_type = self.values[0]
        channel  = interaction.channel

        # ── تحديث الفئة ──
        new_category_id = CATEGORY_IDS.get(new_type)
        new_category    = interaction.guild.get_channel(new_category_id) if new_category_id else None

        old_name = channel.name
        # تغيير اسم القناة إن كانت "شراء-" أو "تذكرة-"
        ticket_num_part = old_name.split("-")[-1] if "-" in old_name else old_name
        new_channel_name = f"{'شراء' if new_type == 'شراء' else 'تذكرة'}-{ticket_num_part}"

        try:
            await channel.edit(
                name=new_channel_name,
                category=new_category,
                overwrites=channel.overwrites  # الصلاحيات تنتقل كما هي
            )
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ فشل التحويل: {e}", ephemeral=True)

        # ── تحديث active_tickets ──
        if channel.id in active_tickets:
            active_tickets[channel.id]["type"] = new_type

        # ── تحديث controls ──
        self.controls.ticket_type = new_type

        # ── حفظ سجل التحويل ──
        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect("nova_tickets.db") as db:
            await db.execute(
                """INSERT INTO transfer_log (ticket_name, from_type, to_type, transferred_by, transferred_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (new_channel_name, self.current_type, new_type, interaction.user.id, now_str)
            )
            await db.commit()

        # ── إشعار في اللوق ──
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(title="🔀 تحويل تذكرة", color=discord.Color.blue())
            log_embed.add_field(name="التذكرة",      value=new_channel_name,         inline=True)
            log_embed.add_field(name="من قسم",       value=self.current_type,         inline=True)
            log_embed.add_field(name="إلى قسم",      value=new_type,                  inline=True)
            log_embed.add_field(name="حوّلها",        value=interaction.user.mention,  inline=True)
            log_embed.add_field(name="الوقت",         value=now_str,                   inline=True)
            log_embed.set_thumbnail(url=LOGO_URL)
            await log_channel.send(embed=log_embed)

        await interaction.response.edit_message(
            content=f"✅ تم تحويل التذكرة من **{self.current_type}** إلى **{new_type}** بنجاح.",
            view=None
        )
        await channel.send(
            f"🔀 تم تحويل التذكرة من قسم **{self.current_type}** إلى قسم **{new_type}** بواسطة {interaction.user.mention}."
        )


class TransferTypeView(discord.ui.View):
    def __init__(self, controls, current_type: str):
        super().__init__(timeout=60)
        self.add_item(TransferTypeSelect(controls, current_type))


# ==========================================
# 🎫 دوال مشتركة للتذكرة
# ==========================================
async def do_close_ticket(interaction: discord.Interaction, controls, close_reason: str = None):
    # إذا كانت الإجابة لم تُرسَل بعد (من modal)
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    transcript      = await chat_exporter.export(interaction.channel)
    transcript_file = discord.File(
        io.BytesIO((transcript or "لا توجد رسائل").encode()),
        filename=f"{interaction.channel.name}.html"
    )

    log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title="📁 تذكرة مغلقة", color=discord.Color.red())
        embed.add_field(name="التذكرة", value=interaction.channel.name)
        embed.add_field(name="النوع",   value=controls.ticket_type)
        embed.add_field(name="بواسطة",  value=interaction.user.mention)
        if close_reason:
            embed.add_field(name="📝 سبب الإغلاق", value=close_reason, inline=False)
        await log_channel.send(embed=embed, file=transcript_file)

    now_utc     = datetime.datetime.now(datetime.timezone.utc)
    closed_at   = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    ticket_info = active_tickets.get(interaction.channel.id, {})
    opened_at_dt= ticket_info.get("opened_at", now_utc)
    resp_secs   = int((now_utc - opened_at_dt).total_seconds())
    t_num       = ticket_info.get("ticket_num", 0)

    async with aiosqlite.connect("nova_tickets.db") as db:
        await db.execute(
            "INSERT OR IGNORE INTO staff_stats (staff_id, claimed, closed) VALUES (?, 0, 0)",
            (interaction.user.id,)
        )
        await db.execute(
            "UPDATE staff_stats SET closed = closed + 1 WHERE staff_id = ?",
            (interaction.user.id,)
        )
        claimed_id = controls.claimed_by.id if controls.claimed_by else None
        await db.execute(
            """INSERT INTO ticket_history
               (ticket_num, user_id, ticket_type, opened_by, claimed_by, closed_by,
                opened_at, closed_at, response_seconds, close_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (t_num, controls.ticket_owner_id, controls.ticket_type,
             controls.ticket_owner_id, claimed_id, interaction.user.id,
             opened_at_dt.strftime("%Y-%m-%d %H:%M:%S"), closed_at, resp_secs,
             close_reason or "")
        )
        await db.commit()

    owner = interaction.guild.get_member(controls.ticket_owner_id)
    if owner:
        try:
            rate_embed = discord.Embed(
                title="عزيزي العميل، تم إغلاق تذكرتك",
                description="نرجو منك تقييم مستوى الدعم الفني الذي حصلت عليه:",
                color=discord.Color.green()
            )
            await owner.send(
                embed=rate_embed,
                view=RatingView(interaction.channel.name, controls.claimed_by or interaction.user)
            )
        except discord.Forbidden:
            pass

    active_tickets.pop(interaction.channel.id, None)

    if interaction.channel.id in voice_tickets:
        v_channel = interaction.guild.get_channel(voice_tickets.pop(interaction.channel.id))
        if v_channel:
            try:
                await v_channel.delete()
            except discord.HTTPException:
                pass

    await interaction.channel.delete()


class AddMemberModal(discord.ui.Modal, title="➕ إضافة شخص للتذكرة"):
    user_id_input = discord.ui.TextInput(
        label="ID الشخص المراد إضافته",
        placeholder="مثال: 123456789012345678",
        required=True, max_length=20
    )
    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid    = int(self.user_id_input.value.strip())
            member = interaction.guild.get_member(uid)
            if not member:
                return await interaction.response.send_message("❌ لم يُعثر على العضو.", ephemeral=True)
            overwrites = dict(self.channel.overwrites)
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            await self.channel.edit(overwrites=overwrites)
            await interaction.response.send_message(f"✅ تم إضافة {member.mention}.", ephemeral=True)
            await self.channel.send(f"➕ تم إضافة {member.mention} للتذكرة بواسطة {interaction.user.mention}.")
        except ValueError:
            await interaction.response.send_message("❌ ID غير صحيح.", ephemeral=True)


class RemoveMemberModal(discord.ui.Modal, title="➖ إزالة شخص من التذكرة"):
    user_id_input = discord.ui.TextInput(
        label="ID الشخص المراد إزالته",
        placeholder="مثال: 123456789012345678",
        required=True, max_length=20
    )
    def __init__(self, channel, owner_id):
        super().__init__()
        self.channel  = channel
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid = int(self.user_id_input.value.strip())
            if uid == self.owner_id:
                return await interaction.response.send_message("❌ لا يمكن إزالة صاحب التذكرة.", ephemeral=True)
            member = interaction.guild.get_member(uid)
            if not member:
                return await interaction.response.send_message("❌ لم يُعثر على العضو.", ephemeral=True)
            overwrites = dict(self.channel.overwrites)
            overwrites[member] = discord.PermissionOverwrite(view_channel=False)
            await self.channel.edit(overwrites=overwrites)
            await interaction.response.send_message(f"✅ تم إزالة {member.mention}.", ephemeral=True)
            await self.channel.send(f"➖ تم إزالة {member.mention} من التذكرة بواسطة {interaction.user.mention}.")
        except ValueError:
            await interaction.response.send_message("❌ ID غير صحيح.", ephemeral=True)


# ==========================================
# 📋 قائمة خيارات الإداري
# ==========================================
class StaffOptionsSelect(discord.ui.Select):
    def __init__(self, controls):
        self.controls = controls
        options = [
            discord.SelectOption(label="🔒 إغلاق التذكرة",    value="close",         description="إغلاق وحفظ السجل أو مع سبب"),
            discord.SelectOption(label="🙋 استلام التذكرة",    value="claim",         description="استلام التذكرة لنفسك"),
            discord.SelectOption(label="🔀 تحويل التذكرة",     value="transfer",      description="تحويل التذكرة لقسم آخر"),
            discord.SelectOption(label="🔊 روم صوتي",           value="voice",         description="إنشاء روم صوتي خاص"),
            discord.SelectOption(label="🔍 فحص العميل",         value="check_user",    description="عرض ملف العميل السري"),
            discord.SelectOption(label="📝 ملاحظة سرية",        value="note",          description="إضافة ملاحظة لا يراها العميل"),
            discord.SelectOption(label="📢 تذكير المواطن",      value="ping_user",     description="تذكير صاحب التذكرة"),
            discord.SelectOption(label="➕ إضافة شخص",           value="add_member",    description="إضافة شخص للتذكرة"),
            discord.SelectOption(label="➖ إزالة شخص",           value="remove_member", description="إزالة شخص من التذكرة"),
        ]
        super().__init__(
            placeholder="⚙️ اختر إجراء...",
            min_values=1, max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if not is_staff(interaction):
            return await interaction.response.send_message("❌ للإدارة فقط!", ephemeral=True)

        val = self.values[0]

        # ── إغلاق ──
        if val == "close":
            await interaction.response.send_message(
                "🔒 اختر طريقة الإغلاق:",
                ephemeral=True,
                view=CloseTypeView(self.controls)
            )

        # ── استلام ──
        elif val == "claim":
            if self.controls.claimed_by:
                return await interaction.response.send_message(
                    f"❌ التذكرة مستلمة بالفعل من {self.controls.claimed_by.mention}.", ephemeral=True
                )
            self.controls.claimed_by = interaction.user
            staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
            overwrites = dict(interaction.channel.overwrites)
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(view_channel=False)
            overwrites[interaction.user] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            await interaction.channel.edit(overwrites=overwrites)
            async with aiosqlite.connect("nova_tickets.db") as db:
                await db.execute(
                    "INSERT OR IGNORE INTO staff_stats (staff_id, claimed, closed) VALUES (?, 0, 0)",
                    (interaction.user.id,)
                )
                await db.execute(
                    "UPDATE staff_stats SET claimed = claimed + 1 WHERE staff_id = ?",
                    (interaction.user.id,)
                )
                await db.commit()
            await interaction.response.send_message("✅ تم الاستلام.", ephemeral=True)
            await interaction.channel.send(f"🛡️ تم استلام التذكرة بواسطة {interaction.user.mention}.")

        # ── تحويل ──
        elif val == "transfer":
            await interaction.response.send_message(
                "🔀 اختر القسم الجديد:",
                ephemeral=True,
                view=TransferTypeView(self.controls, self.controls.ticket_type)
            )

        # ── روم صوتي ──
        elif val == "voice":
            if interaction.channel.id in voice_tickets:
                return await interaction.response.send_message("❌ يوجد روم صوتي بالفعل!", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            owner = interaction.guild.get_member(self.controls.ticket_owner_id)
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user:               discord.PermissionOverwrite(view_channel=True, connect=True),
            }
            if owner:
                overwrites[owner] = discord.PermissionOverwrite(view_channel=True, connect=True)
            v_channel = await interaction.guild.create_voice_channel(
                name=f"صوتي-{interaction.channel.name}",
                category=interaction.channel.category,
                overwrites=overwrites
            )
            voice_tickets[interaction.channel.id] = v_channel.id
            await interaction.channel.send(f"🎙️ تم إنشاء الروم الصوتي: {v_channel.mention}")
            await interaction.followup.send("✅ تم إنشاء الروم الصوتي.", ephemeral=True)

        # ── فحص العميل ──
        elif val == "check_user":
            await interaction.response.defer(ephemeral=True)
            tid = self.controls.ticket_owner_id
            async with aiosqlite.connect("nova_tickets.db") as db:
                async with db.execute("SELECT total_opened FROM user_stats WHERE user_id = ?", (tid,)) as c:
                    row = await c.fetchone()
                    opened_tickets = row[0] if row else 0
                async with db.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (tid,)) as c:
                    is_blacklisted = "⚠️ نعم (محظور)" if await c.fetchone() else "✅ لا"
                async with db.execute("SELECT COUNT(*) FROM user_timeouts WHERE user_id = ?", (tid,)) as c:
                    timeout_count = (await c.fetchone())[0]
                async with db.execute(
                    "SELECT COUNT(*) FROM ticket_history WHERE user_id = ? AND closed_by = -1", (tid,)
                ) as c:
                    auto_closed = (await c.fetchone())[0]

            target_user = interaction.guild.get_member(tid)
            if target_user:
                user_name  = f"{target_user.name}#{target_user.discriminator}" if target_user.discriminator != "0" else target_user.name
                join_date  = target_user.joined_at.strftime("%Y/%m/%d %H:%M")
                acc_created= target_user.created_at.strftime("%Y/%m/%d")
                avatar_url = target_user.display_avatar.url
                is_timed_out = target_user.is_timed_out()
                timeout_str  = "🔇 نعم (موقوف حالياً)" if is_timed_out else "✅ لا"
                top_role = target_user.top_role.mention if target_user.top_role.name != "@everyone" else "لا يوجد"
            else:
                user_name = "مستخدم غادر السيرفر"
                join_date = acc_created = avatar_url = "غير معروف"
                timeout_str = top_role = "غير معروف"

            embed = discord.Embed(title="🔍 ملف العميل السري | NOVA TEAM", color=discord.Color.red())
            embed.set_thumbnail(url=avatar_url if avatar_url != "غير معروف" else LOGO_URL)
            embed.add_field(name="👤 الاسم",            value=f"`{user_name}`",               inline=True)
            embed.add_field(name="🆔 الـ ID",            value=f"`{tid}`",                     inline=True)
            embed.add_field(name="📅 انضم للسيرفر",     value=join_date,                      inline=True)
            embed.add_field(name="🗓️ أنشأ الحساب",     value=acc_created,                    inline=True)
            embed.add_field(name="🚫 محظور من التذاكر", value=is_blacklisted,                 inline=True)
            embed.add_field(name="🔇 تايم اوت حالي",   value=timeout_str,                    inline=True)
            embed.add_field(name="⏱️ مرات التايم اوت", value=f"**{timeout_count}** مرة",     inline=True)
            embed.add_field(name="🕒 تذاكر تايم اوت",  value=f"**{auto_closed}** تذكرة",     inline=True)
            embed.add_field(name="🎫 إجمالي التذاكر",   value=f"**{opened_tickets}** تذكرة", inline=True)
            embed.add_field(name="🏷️ أعلى رول",        value=top_role,                       inline=False)
            embed.add_field(name="📋 نسخ الـ ID",       value=f"```{tid}```",                 inline=False)
            embed.set_footer(text="سرّي للغاية • مخصص لطاقم الدعم فقط", icon_url=LOGO_URL)
            await interaction.followup.send(embed=embed, ephemeral=True)

        elif val == "note":
            await interaction.response.send_modal(NoteModal(ticket_name=interaction.channel.name))

        elif val == "add_member":
            await interaction.response.send_modal(AddMemberModal(channel=interaction.channel))

        elif val == "remove_member":
            await interaction.response.send_modal(RemoveMemberModal(
                channel=interaction.channel,
                owner_id=self.controls.ticket_owner_id
            ))

        elif val == "ping_user":
            owner = interaction.guild.get_member(self.controls.ticket_owner_id)
            if not owner:
                return await interaction.response.send_message("❌ لم يُعثر على صاحب التذكرة.", ephemeral=True)
            await interaction.response.send_message("✅ تم إرسال التذكير.", ephemeral=True)
            dm_embed = discord.Embed(
                title="🔔 تذكير من الطاقم",
                description=f"الطاقم ينتظر ردك في تذكرتك، الرجاء التواصل قريباً.\n\n📌 **التذكرة:** {interaction.channel.mention}",
                color=discord.Color.orange()
            )
            dm_embed.set_footer(text="NOVA TEAM", icon_url=LOGO_URL)
            dm_sent = False
            try:
                await owner.send(embed=dm_embed)
                dm_sent = True
            except discord.Forbidden:
                pass
            if dm_sent:
                await interaction.channel.send(f"{owner.mention} 🔔 تم إرسال تذكير لك في الخاص.")
            else:
                await interaction.channel.send(
                    f"{owner.mention} 🔔 الطاقم ينتظر ردك، الرجاء التواصل قريباً. *(تعذّر إرسال رسالة خاصة — الخاص مقفل)*"
                )


# ==========================================
# 👤 قائمة خيارات المواطن
# ==========================================
class UserOptionsSelect(discord.ui.Select):
    def __init__(self, controls):
        self.controls = controls
        options = [
            discord.SelectOption(label="🔒 إغلاق التذكرة", value="close", description="إغلاق تذكرتك"),
            discord.SelectOption(label="📢 تذكير الطاقم",   value="ping",  description="تذكير الإداري (كل 10 دقائق)"),
        ]
        super().__init__(
            placeholder="👤 خيارات التذكرة...",
            min_values=1, max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]

        if val == "close":
            is_owner = interaction.user.id == self.controls.ticket_owner_id
            if not is_owner and not is_staff(interaction):
                return await interaction.response.send_message("❌ يمكنك إغلاق تذكرتك الخاصة فقط.", ephemeral=True)
            # المستخدم يغلق مباشرة بدون خيارين
            await do_close_ticket(interaction, self.controls)

        elif val == "ping":
            now       = datetime.datetime.now(datetime.timezone.utc)
            last_ping = self.controls.last_ping.get(interaction.channel.id)
            if last_ping and (now - last_ping).total_seconds() < 600:
                remaining = int(600 - (now - last_ping).total_seconds())
                mins = remaining // 60
                secs = remaining % 60
                return await interaction.response.send_message(
                    f"⚠️ يرجى الانتظار {mins}د {secs}ث قبل إعادة التذكير.", ephemeral=True
                )
            await interaction.response.send_message("✅ تم إرسال التذكير للطاقم.", ephemeral=True)
            self.controls.last_ping[interaction.channel.id] = now
            dm_embed = discord.Embed(
                title="🔔 العميل ينتظر ردك",
                description=f"العميل {interaction.user.mention} ينتظر المساعدة في التذكرة.\n\n📌 **التذكرة:** {interaction.channel.mention}",
                color=discord.Color.red()
            )
            dm_embed.set_footer(text="NOVA TEAM", icon_url=LOGO_URL)
            staff_target = self.controls.claimed_by
            dm_sent = False
            if staff_target:
                try:
                    await staff_target.send(embed=dm_embed)
                    dm_sent = True
                except discord.Forbidden:
                    pass
            if dm_sent:
                await interaction.channel.send(
                    f"<@&{STAFF_ROLE_ID}>، العميل {interaction.user.mention} ينتظر المساعدة! *(تم إرسال DM للإداري المستلم)*"
                )
            else:
                await interaction.channel.send(
                    f"<@&{STAFF_ROLE_ID}>، العميل {interaction.user.mention} ينتظر المساعدة!"
                )


# ==========================================
# 🎫 View رئيسي للتذكرة
# ==========================================
class TicketControls(discord.ui.View):
    def __init__(self, ticket_owner_id: int, ticket_type: str):
        super().__init__(timeout=None)
        self.ticket_owner_id = ticket_owner_id
        self.ticket_type     = ticket_type
        self.claimed_by      = None
        self.last_ping: dict[int, datetime.datetime] = {}

    @discord.ui.button(
        label="🎫 Ticket Options",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_staff_options",
        row=0
    )
    async def btn_staff(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            return await interaction.response.send_message("❌ هذا الزر للإدارة فقط.", ephemeral=True)
        await interaction.response.send_message(
            "⚙️ اختر الإجراء:", ephemeral=True,
            view=_StaffMenuView(self)
        )

    @discord.ui.button(
        label="💼 Claim",
        style=discord.ButtonStyle.primary,
        custom_id="btn_claim_shortcut",
        row=0
    )
    async def btn_claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            return await interaction.response.send_message("❌ للإدارة فقط!", ephemeral=True)
        # ✅ منع الإداري الثاني من الاستلام
        if self.claimed_by:
            return await interaction.response.send_message(
                f"❌ مستلمة بالفعل من {self.claimed_by.mention}.", ephemeral=True
            )
        self.claimed_by = interaction.user
        staff_role  = interaction.guild.get_role(STAFF_ROLE_ID)
        overwrites  = dict(interaction.channel.overwrites)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=False)
        overwrites[interaction.user] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        await interaction.channel.edit(overwrites=overwrites)
        async with aiosqlite.connect("nova_tickets.db") as db:
            await db.execute(
                "INSERT OR IGNORE INTO staff_stats (staff_id, claimed, closed) VALUES (?, 0, 0)",
                (interaction.user.id,)
            )
            await db.execute(
                "UPDATE staff_stats SET claimed = claimed + 1 WHERE staff_id = ?",
                (interaction.user.id,)
            )
            await db.commit()
        button.disabled = True
        button.label    = f"✅ {interaction.user.display_name}"
        button.style    = discord.ButtonStyle.success
        await interaction.response.edit_message(view=self)
        await interaction.channel.send(f"🛡️ تم استلام التذكرة بواسطة {interaction.user.mention}.")

    @discord.ui.button(
        label="👤 User Options",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_user_options",
        row=0
    )
    async def btn_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "👤 اختر من الخيارات:", ephemeral=True,
            view=_UserMenuView(self)
        )


class _StaffMenuView(discord.ui.View):
    def __init__(self, controls):
        super().__init__(timeout=60)
        self.add_item(StaffOptionsSelect(controls))


class _UserMenuView(discord.ui.View):
    def __init__(self, controls):
        super().__init__(timeout=60)
        self.add_item(UserOptionsSelect(controls))


# ==========================================
# 📝 نافذة فتح التذكرة
# ==========================================
class TicketModal(discord.ui.Modal, title="تفاصيل التذكرة"):
    reason = discord.ui.TextInput(
        label="ما هو سبب فتح التذكرة؟",
        style=discord.TextStyle.paragraph,
        required=True
    )

    def __init__(self, department: str):
        super().__init__()
        self.department = department

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # ✅ منع فتح أكثر من تذكرة — مع ذكر رابط التذكرة المفتوحة
        for cid, t_info in active_tickets.items():
            if t_info["user_id"] == interaction.user.id:
                existing_channel = interaction.guild.get_channel(cid)
                if existing_channel:
                    return await interaction.edit_original_response(
                        content=f"❌ لديك تذكرة مفتوحة مسبقاً: {existing_channel.mention}\nالرجاء إغلاقها أولاً قبل فتح تذكرة جديدة."
                    )
                else:
                    # القناة اختفت لكن السجل ما اتحذف — ننظف
                    active_tickets.pop(cid, None)
                    break

        ticket_num   = await get_next_ticket_num()
        channel_name = f"{'شراء' if self.department == 'شراء' else 'تذكرة'}-{ticket_num:04d}"

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user:               discord.PermissionOverwrite(view_channel=True, send_messages=True),
            interaction.guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }

        # ✅ إضافة صلاحية الستاف رول بشكل صريح
        staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        category = interaction.guild.get_channel(
            CATEGORY_IDS.get(self.department, list(CATEGORY_IDS.values())[0])
        )
        channel = await interaction.guild.create_text_channel(
            name=channel_name, category=category, overwrites=overwrites
        )

        opened_at = datetime.datetime.now(datetime.timezone.utc)
        active_tickets[channel.id] = {
            "user_id":    interaction.user.id,
            "type":       self.department,
            "last_msg":   opened_at,
            "opened_at":  opened_at,
            "ticket_num": ticket_num,
        }

        async with aiosqlite.connect("nova_tickets.db") as db:
            await db.execute(
                "INSERT OR IGNORE INTO user_stats (user_id, total_opened) VALUES (?, 0)",
                (interaction.user.id,)
            )
            await db.execute(
                "UPDATE user_stats SET total_opened = total_opened + 1 WHERE user_id = ?",
                (interaction.user.id,)
            )
            await db.commit()

        tz_plus3 = datetime.timezone(datetime.timedelta(hours=3))
        now_str  = datetime.datetime.now(tz_plus3).strftime("%A, %B %d, %Y %I:%M %p")

        embed = discord.Embed(color=discord.Color.red())
        embed.add_field(name="[ 🙍 ] : Ticket Owner",   value=interaction.user.mention,  inline=True)
        embed.add_field(name="[ 🛡️ ] : Ticket Admins", value=f"<@&{STAFF_ROLE_ID}>",    inline=True)
        embed.add_field(name="[ 📅 ] : Ticket Date",    value=now_str,                   inline=False)
        embed.add_field(name="[ 🔢 ] : Ticket Number",  value=f"```{ticket_num}```",     inline=True)
        embed.add_field(name="[ ❓ ] : Ticket Section", value=f"```{self.department}```", inline=True)
        embed.add_field(name="[ 📝 ] : Ticket Reason",  value=f"```{self.reason.value}```", inline=False)
        embed.set_image(url="https://cdn.discordapp.com/banners/1363117810389094430/d9bc89a8a86b360ed2a4d3e646213a8c.webp?size=1024")
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        controls = TicketControls(interaction.user.id, self.department)
        await channel.send(
            f"{interaction.user.mention} | <@&{STAFF_ROLE_ID}>",
            embed=embed,
            view=controls
        )

        await interaction.edit_original_response(content=f"✅ تم فتح تذكرتك: {channel.mention}")


# ==========================================
# 🎛️ القائمة المنسدلة لفتح التذكرة
# ==========================================
class TicketDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="مشكلة",   description="مشكلة",   emoji="🛠️", value="مشكلة"),
            discord.SelectOption(label="استفسار", description="استفسار", emoji="❓", value="استفسار"),
            discord.SelectOption(label="شراء",    description="شراء",    emoji="💳", value="شراء"),
        ]
        super().__init__(
            placeholder="اختر القسم المناسب...",
            min_values=1, max_values=1,
            options=options,
            custom_id="ticket_dropdown"
        )

    async def callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect("nova_tickets.db") as db:
            async with db.execute(
                "SELECT user_id FROM blacklist WHERE user_id = ?", (interaction.user.id,)
            ) as cursor:
                if await cursor.fetchone():
                    return await interaction.response.send_message(
                        "❌ أنت ممنوع من فتح التذاكر.", ephemeral=True
                    )

        # فحص التذكرة المفتوحة مسبقاً مع إرسال الرابط
        for cid, t_info in active_tickets.items():
            if t_info["user_id"] == interaction.user.id:
                existing_channel = interaction.guild.get_channel(cid)
                if existing_channel:
                    return await interaction.response.send_message(
                        f"❌ لديك تذكرة مفتوحة مسبقاً: {existing_channel.mention}\nأغلقها أولاً قبل فتح تذكرة جديدة.",
                        ephemeral=True
                    )
                else:
                    active_tickets.pop(cid, None)
                    break

        await interaction.response.send_modal(TicketModal(department=self.values[0]))


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketDropdown())


# ==========================================
# 🚀 الأحداث
# ==========================================
@bot.event
async def on_ready():
    await setup_db()
    bot.add_view(TicketPanelView())
    bot.add_view(TicketControls(0, "مشكلة"))
    if not auto_close_task.is_running():
        auto_close_task.start()
    print("✅ NOVA TICKET PRIME is running!")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"❌ Sync error: {e}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.channel.id in active_tickets:
        active_tickets[message.channel.id]["last_msg"] = datetime.datetime.now(datetime.timezone.utc)

        ticket_info = active_tickets[message.channel.id]
        is_owner    = message.author.id == ticket_info["user_id"]
        staff_check = (
            STAFF_ROLE_ID in [r.id for r in message.author.roles]
            or message.author.guild_permissions.administrator
        )

        if is_owner and not staff_check:
            has_mention = (
                len(message.mentions) > 0
                or len(message.role_mentions) > 0
                or message.mention_everyone
            )
            if has_mention:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                await message.channel.send(
                    f"{message.author.mention} ⚠️ لا يمكنك منشن أحد هنا، استخدم زر **👤 User Options** لتذكير الطاقم.",
                    delete_after=6
                )
                return

    await bot.process_commands(message)


# ==========================================
# ⏰ الإغلاق التلقائي (خمول 15 دقيقة)
# ==========================================
@tasks.loop(minutes=1)
async def auto_close_task():
    now = datetime.datetime.now(datetime.timezone.utc)

    for cid, info in list(active_tickets.items()):
        elapsed = (now - info["last_msg"]).total_seconds()

        # تذكير كل 15 دقيقة
        if elapsed > 0 and int(elapsed) % 900 == 0 and int(elapsed) % 900 < 60:
            channel = bot.get_channel(cid)
            if channel:
                owner = channel.guild.get_member(info["user_id"])
                if owner:
                    remind_embed = discord.Embed(
                        title="🔔 تذكير تلقائي",
                        description=(
                            f"{owner.mention} لم يصلنا ردك منذ **{int(elapsed // 60)} دقيقة**.\n"
                            f"التذكرة ستُغلق تلقائياً بعد **{int((900 - (elapsed % 900)) // 60)} دقيقة** إضافية من آخر رسالة."
                        ),
                        color=discord.Color.yellow()
                    )
                    try:
                        await channel.send(embed=remind_embed)
                    except discord.HTTPException:
                        pass

    to_close = [
        (cid, info)
        for cid, info in list(active_tickets.items())
        if info["type"] in ("مشكلة", "استفسار")
        and (now - info["last_msg"]).total_seconds() >= 900
    ]

    for channel_id, info in to_close:
        active_tickets.pop(channel_id, None)
        channel = bot.get_channel(channel_id)
        if not channel:
            continue

        try:
            transcript = await chat_exporter.export(channel)
            t_file = discord.File(
                io.BytesIO((transcript or "").encode()),
                filename=f"{channel.name}_auto_closed.html"
            )
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                emb = discord.Embed(
                    title="🕒 إغلاق تلقائي للخمول",
                    description=f"أغلقت بسبب عدم الرد لمدة 15 دقيقة.\nالنوع: {info['type']}",
                    color=discord.Color.orange()
                )
                await log_channel.send(embed=emb, file=t_file)
        except Exception as e:
            print(f"[auto_close] خطأ: {e}")

        if channel_id in voice_tickets:
            v_chan = bot.get_channel(voice_tickets.pop(channel_id))
            if v_chan:
                try:
                    await v_chan.delete()
                except discord.HTTPException:
                    pass

        try:
            await channel.delete(reason="Auto-closed: 15 mins inactivity")
        except discord.HTTPException as e:
            print(f"[auto_close] خطأ في حذف القناة: {e}")


@auto_close_task.before_loop
async def before_auto_close():
    await bot.wait_until_ready()


# ==========================================
# ⚙️ الأوامر الإدارية
# ==========================================
@bot.tree.command(name="setup_tickets", description="إرسال لوحة التحكم بالتذاكر (للإدارة)")
@app_commands.default_permissions(administrator=True)
async def setup_tickets(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(
        title="**☎️ مركز الدعم الفني | NOVA TEAM**",
        description=(
            "**مرحباً بك في مركز الدعم.**\n\n"
            "**الرجاء اختيار القسم المناسب لمشكلتك لفتح تذكرة.**\n\n"
            "**⚠️ قوانين مركز الدعم:**\n\n"
            "**1 لا نقبل الدفع بالكريدت (Credit).**\n"
            "**2 مافي شي مجاني لا تحرجونا معاكم.**\n"
            "**3 اذا تفتح تكت و ما تكتب شي تايم اوت اسبوع.**\n"
            "**4 تبي تستفسر او تشتري افتح التكت الي فوق.**\n"
            "**5 في حال ما استلمنا رد منك خلال 15 دقيقه يتقفل التكت.**\n"
            "**6 !! الرجاء الاحترام وتقدير الاوقات الي نكون موجودين فيها.**\n\n"
            "**⚠️ ملاحظة: التذاكر من نوع (مشكلة) أو (استفسار) ستغلق تلقائياً إذا لم يتم الرد خلال 15 دقيقة.**"
        ),
        color=discord.Color.red()
    )
    embed.set_image(url="https://cdn.discordapp.com/attachments/1413880454326521967/1426187792794390671/background.png?ex=6a1d7de3&is=6a1c2c63&hm=fbadb1e2907b3f6760d96d499234a650f6a88199bdfba03a773ae0ecf18dc553&.png")
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.followup.send("✅ تم الإرسال.", ephemeral=True)


@bot.tree.command(name="blacklist_ticket", description="حظر/فك حظر عضو من فتح التذاكر")
@app_commands.default_permissions(administrator=True)
async def blacklist_ticket(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer()
    async with aiosqlite.connect("nova_tickets.db") as db:
        async with db.execute("SELECT user_id FROM blacklist WHERE user_id = ?", (member.id,)) as cursor:
            if await cursor.fetchone():
                await db.execute("DELETE FROM blacklist WHERE user_id = ?", (member.id,))
                msg = f"✅ تم فك الحظر عن {member.mention}."
            else:
                await db.execute("INSERT INTO blacklist (user_id) VALUES (?)", (member.id,))
                msg = f"🚫 تم حظر {member.mention} من التذاكر."
        await db.commit()
    await interaction.followup.send(msg)


@bot.tree.command(name="ticket_stats", description="عرض إحصائيات الإداري في التذاكر")
@app_commands.default_permissions(manage_messages=True)
async def ticket_stats(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer()
    target = member or interaction.user
    async with aiosqlite.connect("nova_tickets.db") as db:
        async with db.execute(
            "SELECT claimed, closed FROM staff_stats WHERE staff_id = ?", (target.id,)
        ) as cursor:
            row = await cursor.fetchone()
        # متوسط وقت الإغلاق لهذا الإداري
        async with db.execute(
            "SELECT AVG(response_seconds) FROM ticket_history WHERE closed_by = ? AND response_seconds > 0",
            (target.id,)
        ) as cursor:
            avg_row = await cursor.fetchone()
    if not row:
        return await interaction.followup.send(f"📊 لا توجد بيانات لـ {target.mention} حتى الآن.")
    avg_mins = int((avg_row[0] or 0) // 60)
    embed = discord.Embed(title=f"📊 إحصائيات الدعم | {target.display_name}", color=discord.Color.blue())
    embed.add_field(name="التذاكر المستلمة 🙋‍♂️",  value=f"**{row[0]}**",    inline=True)
    embed.add_field(name="التذاكر المغلقة 🔒",     value=f"**{row[1]}**",    inline=True)
    embed.add_field(name="⏱️ متوسط وقت الإغلاق", value=f"**{avg_mins} دقيقة**", inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.followup.send(embed=embed)


# ==========================================
# 📊 لوحة الإحصائيات /dashboard — مطورة
# ==========================================
@bot.tree.command(name="dashboard", description="لوحة إحصائيات شاملة للتذاكر والطاقم")
@app_commands.default_permissions(manage_messages=True)
async def dashboard(interaction: discord.Interaction):
    await interaction.response.defer()

    now       = datetime.datetime.now(datetime.timezone.utc)
    today     = now.strftime("%Y-%m-%d")
    week_ago  = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    month_ago = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect("nova_tickets.db") as db:
        # إجمالي التذاكر
        async with db.execute("SELECT COUNT(*) FROM ticket_history") as c:
            total_all = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM ticket_history WHERE opened_at LIKE ?", (f"{today}%",)) as c:
            total_today = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM ticket_history WHERE opened_at >= ?", (week_ago,)) as c:
            total_week = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM ticket_history WHERE opened_at >= ?", (month_ago,)) as c:
            total_month = (await c.fetchone())[0]

        # توزيع الأنواع
        async with db.execute(
            "SELECT ticket_type, COUNT(*) FROM ticket_history GROUP BY ticket_type ORDER BY COUNT(*) DESC"
        ) as c:
            type_rows = await c.fetchall()

        # متوسط وقت الإغلاق العام
        async with db.execute("SELECT AVG(response_seconds) FROM ticket_history WHERE response_seconds > 0") as c:
            avg_sec = (await c.fetchone())[0] or 0
        avg_mins = int(avg_sec // 60)

        # ✅ أفضل 5 إداريين بالإغلاق
        async with db.execute(
            "SELECT staff_id, closed FROM staff_stats ORDER BY closed DESC LIMIT 5"
        ) as c:
            top_closed = await c.fetchall()

        # ✅ أقل إداري نشاطاً (بشرط أن يكون له سجل)
        async with db.execute(
            "SELECT staff_id, closed FROM staff_stats WHERE closed > 0 ORDER BY closed ASC LIMIT 1"
        ) as c:
            least_active = await c.fetchone()

        # ✅ متوسط وقت الرد لكل إداري
        async with db.execute(
            """SELECT closed_by, AVG(response_seconds), COUNT(*)
               FROM ticket_history
               WHERE response_seconds > 0 AND closed_by IS NOT NULL
               GROUP BY closed_by
               ORDER BY AVG(response_seconds) ASC
               LIMIT 5"""
        ) as c:
            staff_avg_rows = await c.fetchall()

        # ✅ عدد التذاكر المفتوحة حسب النوع
        open_by_type: dict[str, int] = {}
        for cid, info in active_tickets.items():
            t = info.get("type", "غير معروف")
            open_by_type[t] = open_by_type.get(t, 0) + 1

        open_now = len(active_tickets)

    embed = discord.Embed(
        title="📊 لوحة الإحصائيات | NOVA TEAM",
        color=discord.Color.red(),
        timestamp=now
    )
    embed.set_thumbnail(url=LOGO_URL)

    # ── أرقام التذاكر ──
    stats_text = (
        f"اليوم        : {total_today}\n"
        f"الأسبوع      : {total_week}\n"
        f"الشهر        : {total_month}\n"
        f"الإجمالي     : {total_all}\n"
        f"مفتوحة الآن  : {open_now}"
    )
    embed.add_field(name="🎫 إحصائيات التذاكر", value=f"```{stats_text}```", inline=False)

    # ── التذاكر المفتوحة حسب النوع ──
    if open_by_type:
        open_type_str = "\n".join(f"{t}: {v}" for t, v in open_by_type.items())
        embed.add_field(name="📂 المفتوحة حسب القسم", value=f"```{open_type_str}```", inline=True)

    # ── توزيع أنواع كل التذاكر ──
    if type_rows:
        types_str = "\n".join(f"{t[0]}: {t[1]}" for t in type_rows)
        embed.add_field(name="📂 توزيع الأقسام (الكل)", value=f"```{types_str}```", inline=True)

    # ── متوسط وقت الإغلاق العام ──
    embed.add_field(name="⏱️ متوسط وقت الإغلاق", value=f"```{avg_mins} دقيقة```", inline=True)

    # ── أفضل 5 إداريين إغلاق ──
    if top_closed:
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        lines = []
        for idx, (sid, cnt) in enumerate(top_closed):
            member = interaction.guild.get_member(sid)
            name   = member.display_name if member else f"ID:{sid}"
            lines.append(f"{medals[idx]} {name}: {cnt}")
        embed.add_field(name="🔒 أفضل 5 إداريين (إغلاق)", value="\n".join(lines), inline=False)

    # ── أقل إداري نشاطاً ──
    if least_active:
        member = interaction.guild.get_member(least_active[0])
        name   = member.display_name if member else f"ID:{least_active[0]}"
        embed.add_field(
            name="😴 أقل إداري نشاطاً",
            value=f"**{name}** — {least_active[1]} تذكرة مغلقة",
            inline=True
        )

    # ── متوسط وقت الرد لكل إداري ──
    if staff_avg_rows:
        lines = []
        for sid, avg_s, cnt in staff_avg_rows:
            member = interaction.guild.get_member(sid)
            name   = member.display_name if member else f"ID:{sid}"
            lines.append(f"**{name}**: {int((avg_s or 0) // 60)} دقيقة ({cnt} تذكرة)")
        embed.add_field(
            name="⏱️ متوسط وقت الرد لكل إداري",
            value="\n".join(lines),
            inline=False
        )

    embed.set_footer(text=f"NOVA TEAM • {now.strftime('%Y-%m-%d %H:%M')} UTC", icon_url=LOGO_URL)
    await interaction.followup.send(embed=embed)


# ==========================================
# ➕ إضافة / إزالة شخص من التكت
# ==========================================
@bot.tree.command(name="ticket_add", description="إضافة شخص إلى التذكرة الحالية")
@app_commands.default_permissions(manage_messages=True)
async def ticket_add(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if interaction.channel.id not in active_tickets:
        return await interaction.followup.send("❌ هذه القناة ليست تذكرة نشطة.", ephemeral=True)
    overwrites = dict(interaction.channel.overwrites)
    overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    await interaction.channel.edit(overwrites=overwrites)
    await interaction.followup.send(f"✅ تم إضافة {member.mention} للتذكرة.", ephemeral=True)
    await interaction.channel.send(f"➕ تم إضافة {member.mention} للتذكرة بواسطة {interaction.user.mention}.")


@bot.tree.command(name="ticket_remove", description="إزالة شخص من التذكرة الحالية")
@app_commands.default_permissions(manage_messages=True)
async def ticket_remove(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if interaction.channel.id not in active_tickets:
        return await interaction.followup.send("❌ هذه القناة ليست تذكرة نشطة.", ephemeral=True)
    ticket_info = active_tickets[interaction.channel.id]
    if member.id == ticket_info["user_id"]:
        return await interaction.followup.send("❌ لا يمكن إزالة صاحب التذكرة.", ephemeral=True)
    overwrites = dict(interaction.channel.overwrites)
    overwrites[member] = discord.PermissionOverwrite(view_channel=False)
    await interaction.channel.edit(overwrites=overwrites)
    await interaction.followup.send(f"✅ تم إزالة {member.mention} من التذكرة.", ephemeral=True)
    await interaction.channel.send(f"➖ تم إزالة {member.mention} من التذكرة بواسطة {interaction.user.mention}.")


# ==========================================
# ⭐ أمر /ratings
# ==========================================
@bot.tree.command(name="ratings", description="عرض تقييمات إداري معين")
@app_commands.default_permissions(manage_messages=True)
async def ratings_cmd(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer()

    async with aiosqlite.connect("nova_tickets.db") as db:
        async with db.execute(
            "SELECT stars, feedback, user_id, ticket_name, created_at FROM ratings WHERE staff_id = ? ORDER BY created_at DESC LIMIT 20",
            (member.id,)
        ) as c:
            rows = await c.fetchall()
        async with db.execute(
            "SELECT COUNT(*), AVG(stars) FROM ratings WHERE staff_id = ?", (member.id,)
        ) as c:
            stats = await c.fetchone()

    total_ratings = stats[0] if stats else 0
    avg_stars = round(stats[1], 2) if stats and stats[1] else 0

    if total_ratings == 0:
        return await interaction.followup.send(f"❌ لا توجد تقييمات لـ {member.mention} حتى الآن.")

    stars_dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for r in rows:
        if r[0] in stars_dist:
            stars_dist[r[0]] += 1

    dist_text = " | ".join(f"{'⭐'*k}: {v}" for k, v in stars_dist.items())

    embed = discord.Embed(title=f"⭐ تقييمات {member.display_name}", color=discord.Color.gold())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="📊 إجمالي التقييمات", value=f"**{total_ratings}**", inline=True)
    embed.add_field(name="⭐ متوسط التقييم",    value=f"**{avg_stars} / 5**", inline=True)
    embed.add_field(name="📈 توزيع النجوم",     value=dist_text,              inline=False)

    recent_lines = []
    for r in rows[:5]:
        stars_text = "⭐" * r[0]
        rater = interaction.guild.get_member(r[2])
        rater_name = rater.display_name if rater else f"ID:{r[2]}"
        feedback = r[1][:40] + "..." if r[1] and len(r[1]) > 40 else (r[1] or "—")
        recent_lines.append(f"{stars_text} من **{rater_name}** | {r[3]}\n> {feedback}")

    if recent_lines:
        embed.add_field(name="🕐 آخر التقييمات", value="\n\n".join(recent_lines), inline=False)

    embed.set_footer(text=f"NOVA TEAM • {member.id}", icon_url=LOGO_URL)
    await interaction.followup.send(embed=embed)


# ==========================================
# 🔀 أمر /transfer_log — سجل التحويلات
# ==========================================
@bot.tree.command(name="transfer_log", description="عرض سجل تحويلات التذاكر")
@app_commands.default_permissions(manage_messages=True)
async def transfer_log_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    async with aiosqlite.connect("nova_tickets.db") as db:
        async with db.execute(
            "SELECT ticket_name, from_type, to_type, transferred_by, transferred_at FROM transfer_log ORDER BY id DESC LIMIT 20"
        ) as c:
            rows = await c.fetchall()

    if not rows:
        return await interaction.followup.send("📋 لا توجد تحويلات مسجلة بعد.")

    embed = discord.Embed(title="🔀 سجل تحويلات التذاكر", color=discord.Color.blue())
    embed.set_thumbnail(url=LOGO_URL)

    lines = []
    for ticket_name, from_t, to_t, by_id, at in rows:
        member = interaction.guild.get_member(by_id)
        name   = member.display_name if member else f"ID:{by_id}"
        lines.append(f"**{ticket_name}**: {from_t} ➜ {to_t} | بواسطة {name} | {at}")

    # تقسيم إن طال النص
    chunk = "\n".join(lines[:10])
    embed.add_field(name="آخر 10 تحويلات", value=chunk or "لا يوجد", inline=False)
    embed.set_footer(text="NOVA TEAM", icon_url=LOGO_URL)
    await interaction.followup.send(embed=embed)


# ==========================================
# ▶️ تشغيل البوت
# ==========================================
TOKEN = os.environ.get("DISCORD_TOKEN", "YOUR_TOKEN_HERE")
bot.run(TOKEN)