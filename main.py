import discord
from discord import app_commands
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.utils import rowcol_to_a1
import random
from datetime import datetime
import time
import os
from dotenv import load_dotenv
import asyncio

# --- LOADING ENVIRONMENT VARIABLES ---
load_dotenv()

def sanitize_for_sheets(text: str) -> str:
    """Prefixes a string with a single quote to prevent formula injection in Google Sheets."""
    if text and isinstance(text, str) and text.startswith(('=', '+', '-', '@')):
        return "'" + text
    return text

# --- GOOGLE SHEETS CONFIGURATION ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
GOOGLE_CREDS_FILE = os.getenv("GOOGLE_CREDS_FILE", "credentials.json")
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
client = gspread.authorize(creds)

SHEET_URL = os.getenv("SHEET_URL")
spreadsheet = client.open_by_url(SHEET_URL)

inventory_tab = spreadsheet.worksheet("Inventory")
draft_tab = spreadsheet.worksheet("Draft")
weapons_tab = spreadsheet.worksheet("Weapons")
log_tab = spreadsheet.worksheet("Logs_Gacha")
pool_tab = spreadsheet.worksheet("Pool_Players")

# --- CACHING HEADERS ---
HEADER_POOL = pool_tab.row_values(1)
CACHED_WEAPONS = None

ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

# --- BOT CONFIGURATION ---
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"✅ Slash commands synchronized for {self.user}")

bot = MyBot()

# --- PACK GENERATION LOGIC ---
def generate_player_pack(available_players, team_stats, exclude_ids=None):
    pack = []
    if exclude_ids is None: exclude_ids = []

    # Filtering: Available + Not already seen in the previous pack (Guaranteed different Reroll)
    pool = [p for p in available_players if p['Status'] == 'Available' and str(p['Osu_ID']) not in exclude_ids]
    
    ssrs = [p for p in pool if p['Tier'] == 'SSR']
    srs = [p for p in pool if p['Tier'] == 'SR']
    rs = [p for p in pool if p['Tier'] == 'R']
    
    for i in range(5):
        # SSR PITY RULE: If 8th pack (index 7) and 0 SSR -> Force SSR in slot 1
        force_ssr = (team_stats.get('packs_opened') == 7 and team_stats.get('ssr_count') == 0 and i == 0)
        
        rand = random.random() * 100
        can_get_ssr = (team_stats.get('ssr_count') < 2 and ssrs) # Maximum 2 SSR
        
        if (rand <= 3 or force_ssr) and can_get_ssr:
            player = random.choice(ssrs)
            ssrs.remove(player)
        elif rand <= 31 and srs:
            player = random.choice(srs)
            srs.remove(player)
        elif rs:
            player = random.choice(rs)
            rs.remove(player)
        else:
            remaining = ssrs + srs + rs
            player = random.choice(remaining) if remaining else None
        
        if player: pack.append(player)

    # SR+ GUARANTEE RULE: At least 1 per pack
    has_sr_plus = any(p['Tier'] in ['SSR', 'SR'] for p in pack)
    if not has_sr_plus and (ssrs or srs):
        pool_fix = (ssrs if team_stats.get('ssr_count') < 2 else []) + srs
        if pool_fix and len(pack) > 0:
            pack[-1] = random.choice(pool_fix)
            
    return pack

# --- HELPER: SNAKE DRAFT LOGIC ---
def get_expected_turn(valid_draft_rows):
    N = len(valid_draft_rows)
    if N == 0: return None, 0, 0
    total_opened = sum(int(r[2]) if len(r) > 2 and str(r[2]).isdigit() else 0 for r in valid_draft_rows)
    current_round = total_opened // N
    pos = total_opened % N
    turn_index = pos if current_round % 2 == 0 else (N - 1 - pos)
    return valid_draft_rows[turn_index], total_opened, N

# --- BUTTONS INTERFACE ---
class PackView(discord.ui.View):
    def __init__(self, pack_data, team_id, captain_id):
        super().__init__(timeout=60)
        self.pack_data = pack_data
        self.team_id = team_id
        self.captain_id = captain_id
        self.message = None
        self.picked = False # Anti-double-click security

    async def on_timeout(self):
        if self.picked: return # A choice has already been made manually
        
        # --- AUTO-PICK LOGIC (Highest Tier) ---
        tier_weights = {'SSR': 3, 'SR': 2, 'R': 1}
        best_index = 0
        best_weight = -1
        for i, p in enumerate(self.pack_data):
            w = tier_weights.get(p.get('Tier', 'R'), 0)
            if w > best_weight:
                best_weight = w
                best_index = i
                
        # We launch process_pick without user interaction (is_timeout=True)
        await self.process_pick(None, best_index, is_timeout=True)

    async def process_pick(self, interaction, index: int, is_timeout: bool = False):
        if not is_timeout:
            if self.picked:
                return await interaction.response.send_message("❌ A choice has already been made!", ephemeral=True)
            if interaction.user.id != self.captain_id:
                return await interaction.response.send_message("❌ Not your turn!", ephemeral=True)
            self.picked = True
            # This "acknowledges" the interaction immediately to avoid timeout
            await interaction.response.defer() 
        else:
            if self.picked: return
            self.picked = True
        
        try:
            all_draft_values = await asyncio.to_thread(draft_tab.get_all_values)
            valid_draft_rows = [r for r in all_draft_values[1:] if r and r[0]]
            
            expected_row, total_opened, N = get_expected_turn(valid_draft_rows)
            
            if expected_row:
                if not is_timeout:
                    if str(interaction.user.id) not in expected_row:
                        return await interaction.followup.send("❌ It's not your turn anymore (you may have already confirmed a pick)!", ephemeral=True)

            player = self.pack_data[index]
            # --- GOOGLE SHEETS API OPTIMIZATION ---
            # We retrieve the pool values all at once (Avoids find() + get_all_records())
            all_pool_values = await asyncio.to_thread(pool_tab.get_all_values)
            osu_id_col = HEADER_POOL.index("Osu_ID")
            player_row_idx = next(i + 1 for i, r in enumerate(all_pool_values) if len(r) > osu_id_col and str(r[osu_id_col]) == str(player['Osu_ID']))

            status_idx = HEADER_POOL.index("Status") + 1
            team_assigned_idx = HEADER_POOL.index("Team_Assigned") + 1
            
            # Batch Update: Updates the 2 player cells in a single request
            await asyncio.to_thread(pool_tab.batch_update, [
                {'range': rowcol_to_a1(player_row_idx, status_idx), 'values': [["Drafted"]]},
                {'range': rowcol_to_a1(player_row_idx, team_assigned_idx), 'values': [[self.team_id]]}
            ])
            
            draft_row = next(i + 1 for i, r in enumerate(all_draft_values) if r and r[0] == self.team_id and str(self.captain_id) in r)
            draft_data = all_draft_values[draft_row - 1]
            
            while len(draft_data) < 7:
                draft_data.append("")
            
            current_opened = int(draft_data[2]) if str(draft_data[2]).isdigit() else 0
            new_opened = current_opened + 1
            current_rerolls = int(draft_data[6]) if str(draft_data[6]).isdigit() else 0
            
            # --- ADDING PLAYER TO THE TEAM COLUMN ---
            current_team_str = draft_data[5]
            if current_team_str.strip():
                new_team_str = f"{current_team_str.strip()} | {player['Username']}"
            else:
                new_team_str = player['Username']

            bonus_msg = ""
            if new_opened in [2, 4, 5, 6]:
                current_rerolls += 1
                bonus_msg = f"\n🎁 **Bonus:** +1 Reroll (Pack {new_opened}/8)"

            # Update Draft in 1 API request (Columns C to G = Indices 2 to 6)
            draft_data[2] = new_opened
            draft_data[3] = max(0, 8 - new_opened)
            draft_data[5] = new_team_str
            draft_data[6] = current_rerolls
            await asyncio.to_thread(draft_tab.update, range_name=f"C{draft_row}:G{draft_row}", values=[draft_data[2:7]])
            
            log_user_name = "AUTO-PICK" if is_timeout else interaction.user.name
            log_entry_user = f"{log_user_name} ({self.team_id})"
            await asyncio.to_thread(log_tab.append_row, [str(datetime.now()), sanitize_for_sheets(log_entry_user), player['Username'], "", "JOUEUR"])
            
            # We create the team roster WITHOUT redoing a get_all_records() API call
            team_col = HEADER_POOL.index("Team_Assigned")
            user_col = HEADER_POOL.index("Username")
            tier_col = HEADER_POOL.index("Tier")
            
            members = []
            for r in all_pool_values[1:]:
                if len(r) > team_col and str(r[team_col]) == self.team_id:
                    members.append({'Username': r[user_col], 'Tier': r[tier_col] if len(r) > tier_col else 'R'})
            members.append({'Username': player['Username'], 'Tier': player['Tier']})

            embed_team = discord.Embed(title=f"Roster {self.team_id}", color=discord.Color.blue())
            reroll_status = "🟢" if current_rerolls >= 1 else "🔴"
            embed_team.description = f"Members: {len(members)}/8\n🔄 Rerolls available: {reroll_status} **{current_rerolls}**\n\n" + "".join([f"- {p['Username']} ({p['Tier']})\n" for p in members])
            
            # --- MODIFY FINAL RESPONSE ---
            content_msg = f"⏰ **Time's up!**\n✅ **{player['Username']}** has automatically joined **{self.team_id}**!{bonus_msg}" if is_timeout else f"✅ **{player['Username']}** has joined **{self.team_id}**!{bonus_msg}"

            if is_timeout:
                if self.message:
                    await self.message.edit(content=content_msg, embed=embed_team, view=None)
            else:
                await interaction.edit_original_response(
                    content=content_msg, 
                    embed=embed_team, 
                    view=None
                )
            self.stop()
            
            # --- NEXT TURN NOTIFICATION (SNAKE DRAFT) ---
            # Update values in memory to avoid Sheets API latency!
            row_idx_to_update = next(i for i, r in enumerate(valid_draft_rows) if r[0] == self.team_id)
            
            while len(valid_draft_rows[row_idx_to_update]) < 7:
                valid_draft_rows[row_idx_to_update].append("")
                
            valid_draft_rows[row_idx_to_update][2] = str(new_opened)
            valid_draft_rows[row_idx_to_update][3] = str(max(0, 8 - new_opened))
            valid_draft_rows[row_idx_to_update][5] = new_team_str
            valid_draft_rows[row_idx_to_update][6] = str(current_rerolls)
            
            total_opened_after = total_opened + 1
            
            channel = self.message.channel if is_timeout else interaction.channel
            if total_opened_after >= N * 8:
                await channel.send("🎉 **THE DRAFT IS COMPLETELY FINISHED!** All teams are complete!")
            else:
                current_round = total_opened_after // N
                pos = total_opened_after % N
                turn_index = pos if current_round % 2 == 0 else (N - 1 - pos)
                
                expected_row = valid_draft_rows[turn_index]
                await spawn_pack_for_turn(channel, expected_row)
                
        except Exception as e:
            # In case of error
            if is_timeout:
                if self.message: await self.message.channel.send(f"Technical error (Auto-pick): {e}")
            else:
                await interaction.followup.send(f"Technical error: {e}", ephemeral=True)

    @discord.ui.button(label="Pick 1", style=discord.ButtonStyle.green)
    async def pick_1(self, interaction, button): await self.process_pick(interaction, 0)
    @discord.ui.button(label="Pick 2", style=discord.ButtonStyle.green)
    async def pick_2(self, interaction, button): await self.process_pick(interaction, 1)
    @discord.ui.button(label="Pick 3", style=discord.ButtonStyle.green)
    async def pick_3(self, interaction, button): await self.process_pick(interaction, 2)
    @discord.ui.button(label="Pick 4", style=discord.ButtonStyle.green)
    async def pick_4(self, interaction, button): await self.process_pick(interaction, 3)
    @discord.ui.button(label="Pick 5", style=discord.ButtonStyle.green)
    async def pick_5(self, interaction, button): await self.process_pick(interaction, 4)

    @discord.ui.button(label="🔄 Reroll", style=discord.ButtonStyle.danger)
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.picked:
            return await interaction.response.send_message("❌ A choice has already been made!", ephemeral=True)
        if interaction.user.id != self.captain_id:
            return await interaction.response.send_message("❌ Not your pack!", ephemeral=True)
            
        self.picked = True
        # Defer to avoid the 3-second timeout if Google Sheets is slow to respond
        await interaction.response.defer()
        
        try:
            all_draft_values = await asyncio.to_thread(draft_tab.get_all_values)
            draft_row = next(i + 1 for i, r in enumerate(all_draft_values) if r and r[0] == self.team_id and str(self.captain_id) in r)
            draft_data = all_draft_values[draft_row - 1]
            rerolls_left = int(draft_data[6]) if len(draft_data) > 6 and str(draft_data[6]).isdigit() else 0
            
            if rerolls_left <= 0:
                self.picked = False
                return await interaction.response.send_message("❌ No more rerolls!", ephemeral=True)

            await asyncio.to_thread(draft_tab.update_cell, draft_row, 7, rerolls_left - 1)
            
            all_players = await asyncio.to_thread(pool_tab.get_all_records)
            team_ssr = sum(1 for p in all_players if str(p.get('Team_Assigned')) == self.team_id and p['Tier'] == 'SSR')
            packs_opened = int(draft_data[2]) if len(draft_data) > 2 and draft_data[2] else 0
            
            # Exclusion of current IDs to guarantee a different pack
            current_ids = [str(p['Osu_ID']) for p in self.pack_data]
            new_pack = generate_player_pack(all_players, {'ssr_count': team_ssr, 'packs_opened': packs_opened}, exclude_ids=current_ids)
            
            rerolls_remaining = rerolls_left - 1
            reroll_status = "🟢" if rerolls_remaining >= 1 else "🔴"
            expires_at = int(time.time() + 60)
            embed = discord.Embed(title="🔄 New Draw (Reroll)", color=discord.Color.red())
            embed.description = "".join([f"**{i+1}.** {'🌟' if p['Tier']=='SSR' else '✨' if p['Tier']=='SR' else '⚪'} **{p['Username']}** | {p['Tier']}\n" for i, p in enumerate(new_pack)])
            embed.description += f"\n\n⏳ **Time to choose:** 60 seconds (<t:{expires_at}:T>)"
            embed.description += f"\n\n⏳ **Time remaining:** <t:{expires_at}:R>"
            embed.set_footer(text=f"🔄 Rerolls available: {reroll_status} {rerolls_remaining}")
            
            new_view = PackView(new_pack, self.team_id, self.captain_id)
            await interaction.edit_original_response(embed=embed, view=new_view)
            new_view.message = interaction.message
        except Exception as e:
            self.picked = False
            await interaction.followup.send(f"Reroll Error: {e}", ephemeral=True)

# --- TURN AUTOMATION FUNCTION ---
async def spawn_pack_for_turn(channel, expected_row):
    team_id = expected_row[0]
    packs_opened = int(expected_row[2]) if len(expected_row) > 2 and str(expected_row[2]).isdigit() else 0
    rerolls_left = int(expected_row[6]) if len(expected_row) > 6 and str(expected_row[6]).isdigit() else 0
    
    captain_id_str = next((c for c in expected_row if str(c).isdigit() and len(str(c)) > 15), None)
    captain_id = int(captain_id_str) if captain_id_str else 0
    
    all_players = await asyncio.to_thread(pool_tab.get_all_records)
    team_ssr = sum(1 for p in all_players if str(p.get('Team_Assigned')) == team_id and p['Tier'] == 'SSR')
    
    current_pack = generate_player_pack(all_players, {'ssr_count': team_ssr, 'packs_opened': packs_opened})
    
    if not current_pack:
        return await channel.send(f"⚠️ No more players available for **{team_id}**!")
        
    reroll_status = "🟢" if rerolls_left >= 1 else "🔴"
    expires_at = int(time.time() + 60)
    embed = discord.Embed(title=f"📦 Pack {packs_opened + 1}/8 - {team_id}", color=discord.Color.gold())
    embed.description = "".join([f"**{i+1}.** {'🌟' if p['Tier']=='SSR' else '✨' if p['Tier']=='SR' else '⚪'} **{p['Username']}** | {p['Tier']}\n" for i, p in enumerate(current_pack)])
    embed.description += f"\n\n⏳ **Time to choose:** 60 seconds (<t:{expires_at}:T>)"
    embed.description += f"\n\n⏳ **Time remaining:** <t:{expires_at}:R>"
    embed.set_footer(text=f"🔄 Rerolls available: {reroll_status} {rerolls_left}")
    
    ping_msg = f"➡️ It's team **{team_id}**'s turn to play!\n<@{captain_id}>, make your choice!" if captain_id else f"➡️ It's team **{team_id}**'s turn to play!"
    
    view = PackView(current_pack, team_id, captain_id)
    msg = await channel.send(content=ping_msg, embed=embed, view=view)
    view.message = msg

# --- SLASH COMMANDS ---

@bot.tree.command(name="admin_force_pack", description="👑 Force the current pack to appear (anti-stuck)")
async def admin_force_pack(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        return await interaction.response.send_message("⛔ Access denied.", ephemeral=True)
    await interaction.response.defer()
    try:
        all_draft_values = await asyncio.to_thread(draft_tab.get_all_values)
        valid_draft_rows = [r for r in all_draft_values[1:] if r and r[0]]
        N = len(valid_draft_rows)
        
        expected_row, total_opened, N = get_expected_turn(valid_draft_rows)
        
        if not expected_row:
            return await interaction.followup.send("⚠️ No teams in the draft.")
            
        if total_opened >= N * 8:
            return await interaction.followup.send("⚠️ The draft is completely finished!")
        await interaction.followup.send("✅ Forced pack:")
        await spawn_pack_for_turn(interaction.channel, expected_row)
    except Exception as e: await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="admin_reset_draft", description="⚠️ FULL RESET (BATCH UPDATE)")
async def admin_reset_draft(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        return await interaction.response.send_message("⛔ Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        # Reset Pool
        players = await asyncio.to_thread(pool_tab.get_all_records)
        if players:
            num = len(players)
            header = await asyncio.to_thread(pool_tab.row_values, 1)
            s_col = chr(64 + header.index("Status") + 1)
            t_col = chr(64 + header.index("Team_Assigned") + 1)
            await asyncio.to_thread(pool_tab.update, range_name=f"{s_col}2:{s_col}{num+1}", values=[["Available"] for _ in range(num)])
            await asyncio.to_thread(pool_tab.update, range_name=f"{t_col}2:{t_col}{num+1}", values=[[""] for _ in range(num)])
        
        # Reset Draft
        rows = len(await asyncio.to_thread(draft_tab.get_all_values))
        if rows > 1:
            await asyncio.to_thread(draft_tab.update, range_name=f"C2:C{rows}", values=[[0] for _ in range(rows-1)]) # Opened
            await asyncio.to_thread(draft_tab.update, range_name=f"D2:D{rows}", values=[[8] for _ in range(rows-1)]) # Remaining
            await asyncio.to_thread(draft_tab.update, range_name=f"F2:F{rows}", values=[[""] for _ in range(rows-1)]) # Team list
            await asyncio.to_thread(draft_tab.update, range_name=f"G2:G{rows}", values=[[1] for _ in range(rows-1)]) # Rerolls
        
        # Reset Inventory & Logs
        log_h = await asyncio.to_thread(log_tab.row_values, 1)
        await asyncio.to_thread(log_tab.clear)
        await asyncio.to_thread(log_tab.append_row, log_h)

        await interaction.followup.send("✅ Reset complete!")
    except Exception as e: await interaction.followup.send(f"Error: {e}")

@bot.tree.command(name="start_draft", description="👑 Announces the start of the draft and pings the first player")
async def start_draft(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        return await interaction.response.send_message("⛔ Access denied.", ephemeral=True)
    await interaction.response.defer()
    try:
        all_draft_values = await asyncio.to_thread(draft_tab.get_all_values)
        valid_draft_rows = [r for r in all_draft_values[1:] if r and r[0]]
        
        expected_row, _, _ = get_expected_turn(valid_draft_rows)
        
        if not expected_row:
            return await interaction.followup.send("⚠️ No teams in the draft.")
        
        await interaction.followup.send(f"🏁 **THE DRAFT BEGINS!**")
        await spawn_pack_for_turn(interaction.channel, expected_row)
    except Exception as e: await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="my_team", description="Displays your team")
async def my_team(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    # ephemeral=True ensures that only the player will see the message
    await interaction.response.defer(ephemeral=True)
    try:
        all_draft_values = await asyncio.to_thread(draft_tab.get_all_values)
        # We search for ALL teams associated with this ID (useful for your tests)
        teams = [r for r in all_draft_values[1:] if r and user_id in r]
        
        if not teams:
            return await interaction.followup.send("❌ You are not the captain of any team.", ephemeral=True)
        
        all_players = await asyncio.to_thread(pool_tab.get_all_records)
        embeds = []
        for team_data in teams:
            team_id = team_data[0]
            rerolls_left = int(team_data[6]) if len(team_data) > 6 and str(team_data[6]).isdigit() else 0
            members = [p for p in all_players if str(p.get('Team_Assigned')) == team_id]
            reroll_status = "🟢" if rerolls_left >= 1 else "🔴"
            embed = discord.Embed(title=f"Roster {team_id}", color=discord.Color.blue())
            embed.description = f"Members: {len(members)}/8\n🔄 Rerolls available: {reroll_status} **{rerolls_left}**\n\n" + "".join([f"- {p['Username']} ({p['Tier']})\n" for p in members])
            embeds.append(embed)
            
        # Discord allows a maximum of 10 embeds per message, we secure the sending
        await interaction.followup.send(embeds=embeds[:10], ephemeral=True)
    except Exception as e: await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

@bot.tree.command(name="pull_weapon", description="🗡️ Draws a random weapon from the gacha!")
async def pull_weapon(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    try:
        # 1. Inventory and permission check
        inventory_data = await asyncio.to_thread(inventory_tab.get_all_values)
        if len(inventory_data) < 2:
            return await interaction.followup.send("⚠️ The inventory is empty.", ephemeral=True)
        inventory_header = inventory_data[0]
        # We convert headers to lowercase to avoid case sensitivity issues
        inventory_header_lower = [str(h).strip().lower() for h in inventory_header]
        all_inventory = [dict(zip(inventory_header_lower, row)) for row in inventory_data[1:]]

        team_inventory = None
        team_inventory_row_index = -1

        for i, row in enumerate(all_inventory):
            if str(row.get('discord id', '')).strip() == user_id:
                team_inventory = row
                team_inventory_row_index = i + 2  # +1 pour l'index 0, +1 pour l'en-tête
                break

        if not team_inventory:
            return await interaction.followup.send("❌ You are not a captain or are not registered in the inventory for draws.", ephemeral=True)

        available_packs = int(team_inventory.get('available packs') or '0')
        if available_packs <= 0:
            return await interaction.followup.send("❌ You have no more weapon draws available.", ephemeral=True)

        # 2. Weapon draw logic (Cached)
        global CACHED_WEAPONS
        if CACHED_WEAPONS is None:
            weapons_data = await asyncio.to_thread(weapons_tab.get_all_values)
            if len(weapons_data) < 2:
                return await interaction.followup.send("⚠️ No weapons are configured in the database.", ephemeral=True)
            weapons_header = weapons_data[0]
            CACHED_WEAPONS = [dict(zip(weapons_header, row)) for row in weapons_data[1:]]
        all_weapons = CACHED_WEAPONS

        rarities = ['EX', 'SSR', 'SR', 'R']
        weights = [0.5, 15.5, 34.0, 50.0]
        drawn_rarity = random.choices(rarities, weights=weights, k=1)[0]

        available_weapons = [w for w in all_weapons if w.get('Rarity') == drawn_rarity]
        if not available_weapons:
            return await interaction.followup.send(f"⚠️ No weapon of rarity **{drawn_rarity}** was found in the Sheet!", ephemeral=True)

        drawn_weapon = random.choice(available_weapons)
        weapon_name = drawn_weapon.get('ID', 'Inconnu')

        # 3. Update inventory in Google Sheets
        new_packs = available_packs - 1
        new_weapon_count = int(team_inventory.get('weapon count') or '0') + 1
        current_weapon_list = team_inventory.get('weapon list', '')
        new_weapon_list = f"{current_weapon_list} | {weapon_name}" if current_weapon_list else weapon_name

        try:
            packs_col = inventory_header_lower.index('available packs') + 1
            count_col = inventory_header_lower.index('weapon count') + 1
            list_col = inventory_header_lower.index('weapon list') + 1
        except ValueError:
            missing = [c for c in ['available packs', 'weapon count', 'weapon list'] if c not in inventory_header_lower]
            return await interaction.followup.send(f"❌ Column(s) not found in inventory (check spelling in Google Sheet): {', '.join(missing)}", ephemeral=True)

        await asyncio.to_thread(inventory_tab.batch_update, [
            {'range': rowcol_to_a1(team_inventory_row_index, packs_col), 'values': [[new_packs]]},
            {'range': rowcol_to_a1(team_inventory_row_index, count_col), 'values': [[new_weapon_count]]},
            {'range': rowcol_to_a1(team_inventory_row_index, list_col), 'values': [[new_weapon_list]]},
        ])

        # 4. Logs and result display
        await asyncio.to_thread(log_tab.append_row, [str(datetime.now()), sanitize_for_sheets(interaction.user.name), weapon_name, drawn_weapon.get('Effect', ''), "ARME"])

        rarity_emojis = {"EX": "🔥", "SSR": "🌟", "SR": "✨", "R": "⚪"}
        emoji = rarity_emojis.get(drawn_rarity, "🗡️")

        embed = discord.Embed(title="⚔️ New Weapon Draw!", color=discord.Color.purple())
        embed.description = f"**Congratulations {interaction.user.mention}!** You got:\n\n{emoji} **{weapon_name}** ({drawn_rarity})\n\n📜 **Effect:** {drawn_weapon.get('Effect', 'No effect specified')}"
        embed.set_footer(text=f"Remaining weapon draws: {new_packs}")

        # Display the image if the Image ID column is filled with a link (http/https)
        image_url = str(drawn_weapon.get('Image ID', '')).strip()
        if image_url.startswith('http'):
            embed.set_image(url=image_url)

        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Technical error: {e}", ephemeral=True)

bot.run(os.getenv("DISCORD_TOKEN"))