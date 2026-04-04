import http.server
import socketserver
import threading
import os
import discord
import google.generativeai as genai
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

# --- 1. ENVIRONMENT & AI CONFIGURATION ---
# We load these first to ensure 'genai' is configured before any calls are made.
load_dotenv()
GOOGLE_AI_KEY = os.getenv("GOOGLE_AI_KEY")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Ensure MAX_HISTORY is an integer
raw_history = os.getenv("MAX_HISTORY")
MAX_HISTORY = int(raw_history) if raw_history and raw_history.isdigit() else 10

if GOOGLE_AI_KEY:
    genai.configure(api_key=GOOGLE_AI_KEY)
else:
    print("❌ ERROR: GOOGLE_AI_KEY not found in environment variables.")

# --- 2. RENDER KEEP-ALIVE ---
def run_on_render():
    port = int(os.environ.get("PORT", 10000))
    handler = http.server.SimpleHTTPRequestHandler
    try:
        with socketserver.TCPServer(("", port), handler) as httpd:
            print(f"Keeping Render alive on port {port}")
            httpd.serve_forever()
    except Exception as e:
        print(f"Server error: {e}")

threading.Thread(target=run_on_render, daemon=True).start()

# --- 3. AI SYSTEM PROMPT & MODEL ---
SUMMERIZE_PROMPT = "Give me 5 bullets about"
message_history = {}

text_generation_config = {
    "temperature": 0.9,
    "top_p": 1,
    "top_k": 1,
    "max_output_tokens": 4096,
}

# The system prompt defines your specific Star Wars GM persona and rules.
gemini_system_prompt = """
[Protocol 1: Source Material]
Instructions: You are an expert Game Master for the Star Wars RPG tabletop roleplaying game from Fantasy Flight and Edge Studios. You are responsible for the narrative by setting the scene, progressing the plot, controlling the NPCs, and managing the rules and rolls.
Source Material Access: Only reference content from: Edge of the Empire, Force and Destiny, Age of Rebellion, No Disintegrations, Dawn of Rebellion, Rise of the Separatists, Collapse of the Republic, and Edge of the Empire GM Kit.
Rules: Only use Fantasy Flight / Edge Studio rules. Never use D20 Saga Edition or D6 rules.

[Protocol 2: Campaign Setting]
Time Period: 9 ABY (Post-Empire/New Republic).
Setting: Mos Eisley, Tatooine.
Tone: Gritty and cinematic (Andor), episodic (Mandalorian), character-driven humor.
Profanity: STRICTLY NONE.
Characters: Thabon (Bothan colonist), Zero (Nautolan bounty hunter), and a human smuggler.

[Protocol 3: Mechanical Execution]
Play Style: Turn-based, play-by-post. Maintain strict turn order.
Destiny Points: Every 3 scenes, roll a white Force Die for every player to determine the pool. Show the result.
Initiative: Roll Cool or Vigilance at start of scenes. Declare slots immediately.
NPC Rolls: Roll for NPCs internally; narrate the outcome based on the hidden roll.
Action Economy: 1 Action and 1 Maneuver per turn.
Dice Resolution: Notify Difficulty -> Roll -> List results (e.g., 2 Success, 1 Advantage).

[Protocol 4: Scene Definition]
Scenes must have purpose and conflict. Maintain a running tally of player inventory and wounds/strain.

[Protocol 5: Character Creation Assistant]
Step-Gate: Process ONE category at a time (Species -> Career -> etc.). Wait for player response before moving to the next step.
"""

# We use 'gemini-1.5-flash' to ensure compatibility with system_instruction.
gemini_model = genai.GenerativeModel(
    model_name="gemini-1.5-flash", 
    generation_config=text_generation_config,
    system_instruction=gemini_system_prompt
)

# --- 4. DISCORD BOT CODE ---
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
    if message.author == bot.user or message.mention_everyone:
        return
    asyncio.create_task(process_message(message))

async def process_message(message):
    if bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel):
        cleaned_text = clean_discord_message(message.content)
        async with message.channel.typing():
            # Handle Image Attachments
            if message.attachments:
                for attachment in message.attachments:
                    if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']):
                        await message.add_reaction('🎨')
                        async with aiohttp.ClientSession() as session:
                            async with session.get(attachment.url) as resp:
                                if resp.status != 200:
                                    await message.channel.send('Unable to download the image.')
                                    return
                                image_data = await resp.read()
                                response_text = await generate_response_with_image_and_text(image_data, cleaned_text)
                                await split_and_send_messages(message, response_text, 1700)
                                return
                    else:
                        # Handle non-image attachments (PDFs/Text)
                        await ProcessAttachments(message, cleaned_text)
                        return
            
            # Handle Text Commands and History
            else:
                if "RESET" in cleaned_text.upper() or "CLEAN" in cleaned_text.upper():
                    if message.author.id in message_history:
                        del message_history[message.author.id]
                    await message.channel.send(f"🧼 History Reset for user: {message.author.name}")
                    return

                if extract_url(cleaned_text) is not None:
                    await message.add_reaction('🔗')
                    response_text = await ProcessURL(cleaned_text)
                    await split_and_send_messages(message, response_text, 1700)
                    return

                await message.add_reaction('💬')
                if MAX_HISTORY == 0:
                    response_text = await generate_response_with_text(cleaned_text)
                    await split_and_send_messages(message, response_text, 1700)
                    return

                update_message_history(message.author.id, cleaned_text)
                response_text = await generate_response_with_text(get_formatted_message_history(message.author.id))
                update_message_history(message.author.id, response_text)
                await split_and_send_messages(message, response_text, 1700)

# --- 5. AI GENERATION FUNCTIONS ---

async def generate_response_with_text(message_text):
    try:
        response = gemini_model.generate_content(message_text)
        return response.text
    except Exception as e:
        return "❌ Exception: " + str(e)

async def generate_response_with_image_and_text(image_data, text):
    try:
        image_parts = [{"mime_type": "image/jpeg", "data": image_data}]
        prompt_parts = [image_parts[0], f"\n{text if text else 'What is this a picture of?' }"]
        response = gemini_model.generate_content(prompt_parts)
        return response.text
    except Exception as e:
        return "❌ Exception: " + str(e)

# --- 6. UTILITY FUNCTIONS ---

def update_message_history(user_id, text):
    if user_id in message_history:
        message_history[user_id].append(text)
        if len(message_history[user_id]) > MAX_HISTORY:
            message_history[user_id].pop(0)
    else:
        message_history[user_id] = [text]

def get_formatted_message_history(user_id):
    if user_id in message_history:
        return '\n\n'.join(message_history[user_id])
    return "No messages found for this user."

async def split_and_send_messages(message_system, text, max_length):
    for i in range(0, len(text), max_length):
        await message_system.channel.send(text[i:i+max_length])    

def clean_discord_message(input_string):
    bracket_pattern = re.compile(r'<[^>]+>')
    return bracket_pattern.sub('', input_string)  

async def ProcessURL(message_str):
    url = extract_url(message_str)
    pre_prompt = remove_url(message_str).strip()
    if pre_prompt == "":
        pre_prompt = SUMMERIZE_PROMPT   
    if is_youtube_url(url):
        return await generate_response_with_text(pre_prompt + " " + get_FromVideoID(get_video_id(url)))     
    if url:       
        return await generate_response_with_text(pre_prompt + " " + extract_text_from_url(url))
    return "No URL Found"

def extract_url(string):
    url_regex = re.compile(r'https?://\S+', re.IGNORECASE)
    match = re.search(url_regex, string)
    return match.group(0) if match else None

def remove_url(text):
    return re.sub(r"https?://\S+", "", text)

def extract_text_from_url(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200: return "Failed to retrieve webpage"
        soup = BeautifulSoup(response.text, 'html.parser')
        paragraphs = soup.find_all('p')
        return ' '.join([p.text for p in paragraphs])
    except:
        return "Error scraping URL"

def is_youtube_url(url):
    if not url: return False
    return "youtube.com" in url or "youtu.be" in url

def get_video_id(url):
    parsed_url = urlparse.urlparse(url)
    if "youtube.com" in parsed_url.netloc:
        return urlparse.parse_qs(parsed_url.query).get('v', [None])[0]
    return parsed_url.path[1:] if "youtu.be" in parsed_url.netloc else None

def get_FromVideoID(video_id):
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        return ' '.join([i['text'] for i in transcript_list])
    except:
        return "Error retrieving transcript"

async def ProcessAttachments(message, prompt):
    if not prompt: prompt = SUMMERIZE_PROMPT  
    for attachment in message.attachments:
        await message.add_reaction('📄')
        async with aiohttp.ClientSession() as session:
            async with session.get(attachment.url) as resp:
                if resp.status != 200: continue
                if attachment.filename.lower().endswith('.pdf'):
                    pdf_data = await resp.read()
                    response_text = await process_pdf(pdf_data, prompt)
                else:
                    text_data = await resp.text()
                    response_text = await generate_response_with_text(prompt + ": " + text_data)
                await split_and_send_messages(message, response_text, 1700)

async def process_pdf(pdf_data, prompt):
    doc = fitz.open(stream=pdf_data, filetype="pdf")
    text = "".join([page.get_text() for page in doc])
    doc.close()
    return await generate_response_with_text(prompt + ": " + text)

# --- 7. RUN BOT ---
if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("❌ ERROR: DISCORD_BOT_TOKEN not found.")
