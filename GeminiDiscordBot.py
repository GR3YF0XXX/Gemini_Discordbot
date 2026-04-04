import http.server
import socketserver
import threading
import os
import discord
from google import genai
from google.genai import types
from google.genai.types import HttpOptions
from discord.ext import commands
from pathlib import Path
import aiohttp
import re
import fitz  # PyMuPDF
import asyncio
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled
import urllib.parse as urlparse

# --- Render Keep-Alive Server ---
def run_on_render():
    port = int(os.environ.get("PORT", 10000))
    handler = http.server.SimpleHTTPRequestHandler
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("", port), handler) as httpd:
            print(f"Keeping Render alive on port {port}")
            httpd.serve_forever()
    except Exception as e:
        print(f"Server error: {e}")

threading.Thread(target=run_on_render, daemon=True).start()

# --- Load Environment Variables ---
load_dotenv()
GOOGLE_AI_KEY = os.getenv("GOOGLE_AI_KEY")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", 10))

SUMMERIZE_PROMPT = "Give me 5 bullets about"
message_history = {}

# --- AI Configuration ---
client = genai.Client(
    api_key=GOOGLE_AI_KEY, 
    http_options=HttpOptions(api_version='v1')
)
gemini_model_name = "gemini-1.5-flash"

gemini_system_prompt = """
[Protocol 1: Source Material]
Instructions: You are an expert Game Master for the Star Wars RPG tabletop roleplaying game from Fantasy Flight and Edge Studios. You are responsible for the narrative by setting the scene, progressing the plot, controlling the NPCs, and managing the rules and rolls.
Source Material Access: Only reference content from the following books:
Edge of the Empire Core Book, Force and Destiny Core Book, Age of Rebellion Core Book.
No Disintegrations, Dawn of Rebellion, Rise of the Separatists, Collapse of the Republic.
Edge of the Empire Gamemaster's Kit.
If you introduce species, gear, or vehicles not in these books, skin them from existing content or general Star Wars lore. Do not source mechanics from unowned RPG books.
Rules: Only use Fantasy Flight / Edge Studio Star Wars RPG Roleplaying Game rules. Never use the D20 Star Wars Saga Edition rules from Wizards of the Coast and never use the D6 rules from West End Games.

[Protocol 2: Campaign Setting]
Time Period: 9 ABY (Post-Empire/New Republic era).
Setting: Mos Eisley, Tatooine.
Tone: Gritty and cinematic (like Andor), episodic yet connected (like The Mandalorian), with character-driven humor (like Guardians of the Galaxy).
Profanity: STRICTLY NONE. Do not use swearing, blasphemy, or religious names in vain.
Characters: Thabon, a Bothan colonist, Zero, a Nautolan bounty hunter and a human smuggler.

[Protocol 3: Mechanical Execution]:
Play Style: Turn-based, play-by-post. Maintain strict turn order.
Destiny Points: At the start (and every 3 scenes), roll a white Force Die for every player to determine the pool. Explicitly show the result (Light/Dark).
Derived Systems: Use Obligation, Morality, and Duty based on party size. Trigger these checks using the specific "threshold window" rule.
Initiative: Roll Cool or Vigilance automatically at the start of scenes. Declare the slots and assign players to them immediately.
NPC Rolls: Roll for NPCs internally. Do not show their raw dice results; narrate the outcome based on the hidden roll to maintain immersion.
Action Economy: When it is a player's turn, ask: "[Player Name], what would your character like to attempt? I will tell you which skill is required. Reminder: You have 1 Action and 1 Maneuver (or 2 maneuvers for 2 strain)." Offer to list Talents if needed, but do not suggest specific actions.
Dice Resolution: * Notify the player of the Difficulty and offer Destiny Point spending.
Roll the dice and list the results clearly (e.g., 2 Success, 1 Advantage).
Let the player decide how to spend Advantage/Triumph. Provide tips if they are stuck.
You (the GM) decide how to spend Threat/Despair.

[Protocol 4: Scene Definition and Inventory]
A "Scene" is a continuous unit of storytelling in one location/time. It must have purpose, conflict, and a clear beginning/middle/end.
Inventory: Maintain a running tally of player inventory and wounds/strain.
Session: 1 Session = 3 concluded Scenes.
Operational Rule: Always use Boost and Setback dice based on the environment. Factor in character Talents for every check. Use Opposed, Competitive, and Assisted checks where appropriate.
Initialization Check: Before we begin, please confirm you have loaded these protocols by summarizing the current Destiny Pool (roll it now) and describing the atmosphere of Mos Eisley in 9 ABY.

[Protocol 5: Character Creation Assistant]
Role: You're a Character Creation Assistant for Star Wars RPG (Edge of the Empire, Age of Rebellion, Force and Destiny). Your goal is to guide a player through creating a new character from scratch, ensuring they understand their mechanical and narrative choices at every step.

Operational Rule - The Step-Gate: You must only process ONE category at a time. Do not list the entire process at once. Explain the current category, present the player with their options, and WAIT for their response before moving to the next number in the sequence.

The Character Creation Sequence:
Species: List all the species in the books made available by protocol 1 of 5. Explain the starting wound/strain thresholds for their choice.
Career: Explain the different roles.
Ask name.
Obligation (or Duty/Morality): Suggest one based on their career type. Offer a choice of starting values.
Characteristics: Help them spend their starting XP to increase Brawn, Agility, Intellect, Cunning, Willpower, or Presence. Remind them that this is the most important time to spend XP.
Specialization: Pick the first tree within their Career.
Motivations: Share the options for the rule books.
Skills: Guide them through choosing their free Career and Specialization skills.
Equipment: Start them with 500 Credits (or adjusted based on Obligation) and suggest basic gear.
Vehicles: Discuss if they have access to a group vehicle or personal mount.
Background Beginnings: Share the options for the rule books.
Background Attitude Toward Force: Share the options for the rule books.
Background Reason for Adventure: Share the options for the rule books.

At the end provide full character summary.

Tone & Style: Helpful, encouraging, and knowledgeable. 
Restriction: Only reference content listed in Protocol 1.
"""

# --- AI Generation Functions ---
async def generate_response_with_text(message_text):
    try:
        response = client.models.generate_content(
            model=gemini_model_name,
            contents=message_text,
            config=types.GenerateContentConfig(
                system_instruction=gemini_system_prompt,
                temperature=0.9,
            )
        )
        return response.text
    except Exception as e:
        return "❌ Exception: " + str(e)

async def generate_response_with_image_and_text(image_data, text):
    try:
        image_part = types.Part.from_bytes(data=image_data, mime_type="image/jpeg")
        prompt = text if text else "What is this a picture of?"
        response = client.models.generate_content(
            model=gemini_model_name,
            contents=[image_part, prompt],
            config=types.GenerateContentConfig(
                system_instruction=gemini_system_prompt,
                temperature=0.9,
            )
        )
        return response.text
    except Exception as e:
        return "❌ Exception: " + str(e)

# --- Discord Bot Core ---
defaultIntents = discord.Intents.default()
defaultIntents.message_content = True
bot = commands.Bot(command_prefix="!", intents=defaultIntents)

@bot.event
async def on_ready():
    print("----------------------------------------")
    print(f'Gemini Bot Logged in as {bot.user}')
    print("----------------------------------------")

@bot.event
async def on_message(message):
    asyncio.create_task(process_message(message))

async def process_message(message):
    if message.author == bot.user or message.mention_everyone:
        return

    if bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel):
        cleaned_text = clean_discord_message(message.content)
        async with message.channel.typing():
            if message.attachments:
                for attachment in message.attachments:
                    if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']):
                        await message.add_reaction('🎨')
                        async with aiohttp.ClientSession() as session:
                            async with session.get(attachment.url) as resp:
                                if resp.status != 200:
                                    await message.channel.send('Unable to download image.')
                                    return
                                image_data = await resp.read()
                                response_text = await generate_response_with_image_and_text(image_data, cleaned_text)
                                await split_and_send_messages(message, response_text, 1700)
                                return
                    else:
                        await ProcessAttachments(message, cleaned_text)
                        return
            else:
                if "RESET" in cleaned_text or "CLEAN" in cleaned_text:
                    if message.author.id in message_history:
                        del message_history[message.author.id]
                    await message.channel.send(f"🧼 History Reset for {message.author.name}")
                    return

                if extract_url(cleaned_text):
                    await message.add_reaction('🔗')
                    response_text = await ProcessURL(cleaned_text)
                    await split_and_send_messages(message, response_text, 1700)
                    return

                await message.add_reaction('💬')
                update_message_history(message.author.id, cleaned_text)
                response_text = await generate_response_with_text(get_formatted_message_history(message.author.id))
                update_message_history(message.author.id, response_text)
                await split_and_send_messages(message, response_text, 1700)

# --- Helper Functions ---
def update_message_history(user_id, text):
    if user_id not in message_history:
        message_history[user_id] = []
    message_history[user_id].append(text)
    if len(message_history[user_id]) > MAX_HISTORY:
        message_history[user_id].pop(0)

def get_formatted_message_history(user_id):
    history_list = message_history.get(user_id, [])
    return '\n\n'.join(history_list) if history_list else "No history found."

async def split_and_send_messages(message_system, text, max_length):
    if not text:
        return
    for i in range(0, len(text), max_length):
        await message_system.channel.send(text[i:i+max_length])

def clean_discord_message(input_string):
    return re.sub(r'<[^>]+>', '', input_string).strip()

async def ProcessURL(message_str):
    url = extract_url(message_str)
    pre_prompt = remove_url(message_str) or SUMMERIZE_PROMPT
    if is_youtube_url(url):
        return await generate_response_with_text(f"{pre_prompt} {get_FromVideoID(get_video_id(url))}")
    return await generate_response_with_text(f"{pre_prompt} {extract_text_from_url(url)}")

def extract_url(string):
    match = re.search(r'https?://\S+', string)
    return match.group(0) if match else None

def remove_url(text):
    return re.sub(r"https?://\S+", "", text).strip()

def extract_text_from_url(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        return ' '.join([p.text for p in soup.find_all('p')])
    except:
        return "Failed to scrape text from the provided URL."

def get_video_id(url):
    parsed = urlparse.urlparse(url)
    if "youtube.com" in parsed.netloc:
        return urlparse.parse_qs(parsed.query).get('v', [None])[0]
    return parsed.path[1:] if "youtu.be" in parsed.netloc else None

def is_youtube_url(url):
    return url and ("youtube.com" in url or "youtu.be" in url)

def get_FromVideoID(video_id):
    try:
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        return ' '.join([i['text'] for i in transcript])
    except:
        return "Transcript unavailable for this YouTube video."

async def ProcessAttachments(message, prompt):
    prompt = prompt or SUMMERIZE_PROMPT
    for attachment in message.attachments:
        await message.add_reaction('📄')
        async with aiohttp.ClientSession() as session:
            async with session.get(attachment.url) as resp:
                if attachment.filename.lower().endswith('.pdf'):
                    data = await resp.read()
                    doc = fitz.open(stream=data, filetype="pdf")
                    text = "".join([page.get_text() for page in doc])
                    doc.close()
                    response_text = await generate_response_with_text(f"{prompt}: {text}")
                else:
                    text = await resp.text()
                    response_text = await generate_response_with_text(f"{prompt}: {text}")
                await split_and_send_messages(message, response_text, 1700)

bot.run(DISCORD_BOT_TOKEN)
