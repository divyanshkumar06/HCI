"""
AURA - Elderly Care Voice Assistant (single-file)
Requirements:
  pip install customtkinter pyttsx3 SpeechRecognition wikipedia requests openai pygame pillow
Notes:
  - Ensure microphone is available for speech recognition.
  - Provide OpenWeatherMap and (optionally) NewsAPI / OpenAI keys in Settings to enable those features.
"""

import os
import sys
import json
import time
import queue
import threading
import datetime
import random
import webbrowser
from typing import Dict, List, Optional

import customtkinter as ctk
from tkinter import filedialog, messagebox
import pyttsx3
import speech_recognition as sr
import requests
import wikipedia
import openai
from pygame import mixer

# -------------------------
# CONFIG / DEFAULTS
# -------------------------
CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "user_name": "User",
    "city": "New York",
    "theme": "Dark",             # "Dark" or "Light" or "System"
    "font_size": 16,
    "voice_rate": 150,
    "voice_volume": 1.0,
    "api_keys": {
        "openweathermap": "",
        "newsapi": "",
        "openai": ""
    },
    "favorites": {
        "music": [],
        "websites": []
    },
    "emergency_contacts": [],     # list of {"name": "...", "phone": "...", "relation": "..."}
    "medication_schedule": {}     # {"MedName": ["08:00","20:00"], ...}
}

WEATHER_URL = "http://api.openweathermap.org/data/2.5/weather"
NEWS_URL = "https://newsapi.org/v2/top-headlines"

# -------------------------
# UTILS: Config Manager
# -------------------------
class ConfigManager:
    def __init__(self, path: str = CONFIG_FILE):
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    # merge defaults (shallow)
                    merged = DEFAULT_CONFIG.copy()
                    merged.update(loaded)
                    # ensure nested fields exist
                    if "api_keys" not in merged:
                        merged["api_keys"] = DEFAULT_CONFIG["api_keys"].copy()
                    else:
                        for k, v in DEFAULT_CONFIG["api_keys"].items():
                            merged["api_keys"].setdefault(k, v)
                    if "favorites" not in merged:
                        merged["favorites"] = DEFAULT_CONFIG["favorites"].copy()
                    if "medication_schedule" not in merged:
                        merged["medication_schedule"] = {}
                    if "emergency_contacts" not in merged:
                        merged["emergency_contacts"] = []
                    return merged
            except Exception as e:
                print(f"[Config] Error loading config: {e}")
        return DEFAULT_CONFIG.copy()

    def save(self) -> bool:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4)
            return True
        except Exception as e:
            print(f"[Config] Error saving config: {e}")
            return False

    def get(self, key_path: str, default=None):
        parts = key_path.split(".")
        cur = self.data
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return default
        return cur

    def set(self, key_path: str, value):
        parts = key_path.split(".")
        cur = self.data
        for p in parts[:-1]:
            if p not in cur or not isinstance(cur[p], dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = value

# -------------------------
# CORE: TTS and Mixer init
# -------------------------
# initialize pygame mixer for music
try:
    mixer.init()
except Exception as e:
    print(f"[Audio] mixer.init() failed: {e}")

tts_engine = pyttsx3.init()
# keep reference to voices if needed
voices = tts_engine.getProperty("voices")

# -------------------------
# APP: Main Window
# -------------------------
class AURAApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AURA - Elderly Care Assistant")
        self.geometry("1200x760")
        self.minsize(1000, 650)

        # load config
        self.config_mgr = ConfigManager()
        self.config = self.config_mgr.data

        # apply theme and font size
        try:
            ctk.set_appearance_mode(self.config.get("theme", "Dark"))
        except Exception:
            ctk.set_appearance_mode("Dark")

        self.font_size = int(self.config.get("font_size", 16))

        # apply voice settings
        self.apply_tts_settings()

        # GUI update queue (thread-safe)
        self.gui_queue: queue.Queue = queue.Queue()
        self.after(100, self._process_gui_queue)

        # listening flag
        self.listening_event = threading.Event()

        # medication reminders tracking (store after ids to cancel if needed)
        self._med_after_ids: List[int] = []

        # build UI
        self._build_ui()

        # schedule medication reminders
        self._schedule_medication_reminders()

        # greeting
        self._greet_user()

        # ensure OpenAI key present in library object if present in config
        openai_key = self.config_mgr.get("api_keys.openai", "")
        if openai_key:
            openai.api_key = openai_key

    # -------------------------
    # UI BUILDING
    # -------------------------
    def _build_ui(self):
        # layout: sidebar + main area
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Sidebar
        sidebar = ctk.CTkFrame(self, width=260, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_rowconfigure(9, weight=1)

        logo = ctk.CTkLabel(sidebar, text="AURA", font=ctk.CTkFont(size=28, weight="bold"))
        logo.grid(row=0, column=0, padx=20, pady=(20, 5), sticky="w")

        self.user_label = ctk.CTkLabel(sidebar, text=f"Hello, {self.config.get('user_name', 'User')}", font=ctk.CTkFont(size=14))
        self.user_label.grid(row=1, column=0, padx=20, pady=(0, 15), sticky="w")

        # Buttons
        btn_opts = {"corner_radius": 8, "height": 44, "anchor": "w"}
        self.listen_button = ctk.CTkButton(sidebar, text="🎤 Start Listening", command=self.toggle_listening, **btn_opts)
        self.listen_button.grid(row=2, column=0, padx=16, pady=6, sticky="ew")

        self.play_button = ctk.CTkButton(sidebar, text="▶ Play Music", command=self.play_music_from_favorites, **btn_opts)
        self.play_button.grid(row=3, column=0, padx=16, pady=6, sticky="ew")

        self.weather_button = ctk.CTkButton(sidebar, text="☀ Weather", command=lambda: threading.Thread(target=self._do_command, args=("weather",), daemon=True).start(), **btn_opts)
        self.weather_button.grid(row=4, column=0, padx=16, pady=6, sticky="ew")

        self.news_button = ctk.CTkButton(sidebar, text="📰 News", command=lambda: threading.Thread(target=self._do_command, args=("news",), daemon=True).start(), **btn_opts)
        self.news_button.grid(row=5, column=0, padx=16, pady=6, sticky="ew")

        self.med_button = ctk.CTkButton(sidebar, text="💊 Medications", command=self.show_medication_schedule, **btn_opts)
        self.med_button.grid(row=6, column=0, padx=16, pady=6, sticky="ew")

        self.emergency_button = ctk.CTkButton(sidebar, text="🆘 Emergency", fg_color="#FF6B6B", hover_color="#FF5252", command=self.emergency_protocol, **btn_opts)
        self.emergency_button.grid(row=7, column=0, padx=16, pady=6, sticky="ew")

        self.settings_button = ctk.CTkButton(sidebar, text="⚙ Settings", command=self.open_settings, **btn_opts)
        self.settings_button.grid(row=9, column=0, padx=16, pady=20, sticky="snew")

        # Main content
        main = ctk.CTkFrame(self)
        main.grid(row=0, column=1, sticky="nsew", padx=12, pady=12)
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        # status bar
        self.status_var = ctk.StringVar(value="Ready")
        status = ctk.CTkLabel(main, textvariable=self.status_var, height=36, corner_radius=6)
        status.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        # conversation box
        self.textbox = ctk.CTkTextbox(main, wrap="word", font=ctk.CTkFont(size=self.font_size))
        self.textbox.grid(row=1, column=0, sticky="nsew")
        self.textbox.configure(state="disabled")

        # input frame
        input_frame = ctk.CTkFrame(main, height=70)
        input_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        input_frame.grid_columnconfigure(0, weight=1)

        self.entry = ctk.CTkEntry(input_frame, placeholder_text="Type a message or press the mic...", font=ctk.CTkFont(size=self.font_size))
        self.entry.grid(row=0, column=0, padx=(8, 6), pady=10, sticky="ew")
        self.entry.bind("<Return>", lambda e: self.send_text())

        self.send_btn = ctk.CTkButton(input_frame, text="Send", width=110, command=self.send_text)
        self.send_btn.grid(row=0, column=1, padx=(0, 8), pady=10)

    # -------------------------
    # GUI queue processing
    # -------------------------
    def _process_gui_queue(self):
        try:
            while not self.gui_queue.empty():
                item = self.gui_queue.get_nowait()
                action = item[0]
                if action == "append":
                    tag, text = item[1], item[2]
                    self._append_text(tag, text)
                elif action == "status":
                    self.status_var.set(item[1])
                elif action == "button":
                    btn, text = item[1], item[2]
                    btn.configure(text=text)
                elif action == "error_popup":
                    messagebox.showerror("Error", item[1], parent=self)
                elif action == "info_popup":
                    messagebox.showinfo("Info", item[1], parent=self)
        except Exception as e:
            print(f"[GUI Queue] process error: {e}")
        finally:
            self.after(100, self._process_gui_queue)

    def _append_text(self, tag: str, text: str):
        self.textbox.configure(state="normal")
        if tag == "user":
            self.textbox.insert("end", f"You: {text}\n\n")
        elif tag == "assistant":
            self.textbox.insert("end", f"AURA: {text}\n\n")
        elif tag == "system":
            self.textbox.insert("end", f"System: {text}\n\n")
        elif tag == "emergency":
            self.textbox.insert("end", f"!!! EMERGENCY: {text}\n\n")
        else:
            self.textbox.insert("end", f"{text}\n\n")
        self.textbox.configure(state="disabled")
        self.textbox.see("end")

    # -------------------------
    # Greeting
    # -------------------------
    def _greet_user(self):
        hour = datetime.datetime.now().hour
        if 5 <= hour < 12:
            part = "Good morning"
        elif 12 <= hour < 18:
            part = "Good afternoon"
        else:
            part = "Good evening"
        name = self.config.get("user_name", "User")
        greeting = f"{part}, {name}. I'm AURA — how can I help you today?"
        self.gui_queue.put(("append", "assistant", greeting))
        self.speak(greeting)

    # -------------------------
    # TTS helpers
    # -------------------------
    def apply_tts_settings(self):
        rate = int(self.config.get("voice_rate", 150))
        vol = float(self.config.get("voice_volume", 1.0)) if "voice_volume" in self.config else 1.0
        try:
            tts_engine.setProperty("rate", rate)
            tts_engine.setProperty("volume", vol)
            # optional: set female voice if available
            if len(voices) > 1:
                # keep user preference if saved (not implemented here) otherwise choose index 1
                tts_engine.setProperty("voice", voices[1].id)
        except Exception as e:
            print(f"[TTS] Could not apply settings: {e}")

    def speak(self, text: str, interrupt=False):
        # uses a background thread to avoid blocking
        def _speak():
            try:
                if interrupt:
                    try:
                        tts_engine.stop()
                    except Exception:
                        pass
                tts_engine.say(text)
                tts_engine.runAndWait()
            except Exception as e:
                print(f"[TTS] speak error: {e}")
                self.gui_queue.put(("append", "system", f"Speech error: {e}"))
        threading.Thread(target=_speak, daemon=True).start()

    # -------------------------
    # Listening (speech -> text)
    # -------------------------
    def toggle_listening(self):
        if self.listening_event.is_set():
            self.listening_event.clear()
            self.gui_queue.put(("button", self.listen_button, "🎤 Start Listening"))
            self.gui_queue.put(("status", "Ready"))
        else:
            self.listening_event.set()
            self.gui_queue.put(("button", self.listen_button, "🔴 Listening..."))
            self.gui_queue.put(("status", "Listening..."))
            threading.Thread(target=self._listen_loop, daemon=True).start()

    def _listen_loop(self):
        recognizer = sr.Recognizer()
        mic = None
        try:
            with sr.Microphone() as source:
                while self.listening_event.is_set():
                    self.gui_queue.put(("status", "Listening..."))
                    recognizer.adjust_for_ambient_noise(source, duration=0.5)
                    try:
                        audio = recognizer.listen(source, timeout=5, phrase_time_limit=10)
                    except sr.WaitTimeoutError:
                        continue
                    self.gui_queue.put(("status", "Recognizing..."))
                    try:
                        text = recognizer.recognize_google(audio, language="en-IN")
                        if text:
                            self.gui_queue.put(("append", "user", text))
                            # handle command in background
                            threading.Thread(target=self._do_command, args=(text,), daemon=True).start()
                    except sr.UnknownValueError:
                        self.gui_queue.put(("append", "system", "Could not understand audio."))
                    except sr.RequestError as e:
                        self.gui_queue.put(("append", "system", f"Speech service error: {e}"))
                        self.listening_event.clear()
                        break
        except Exception as e:
            self.gui_queue.put(("error_popup", f"Microphone error: {e}"))
        finally:
            self.listening_event.clear()
            self.gui_queue.put(("button", self.listen_button, "🎤 Start Listening"))
            self.gui_queue.put(("status", "Ready"))

    # -------------------------
    # Commands / Actions
    # -------------------------
    def send_text(self):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.gui_queue.put(("append", "user", text))
        threading.Thread(target=self._do_command, args=(text,), daemon=True).start()

    def _do_command(self, command: str):
        """Main router for text commands. Non-blocking (run in background threads)."""
        try:
            cmd = command.lower().strip()

            # Emergency keywords
            if any(k in cmd for k in ("help", "emergency", "accident", "fall", "ambulance")):
                self.gui_queue.put(("append", "assistant", "Triggering emergency protocol."))
                self.emergency_protocol()
                return

            if "time" in cmd:
                now = datetime.datetime.now().strftime("%I:%M %p")
                resp = f"The time is {now}."
                self.gui_queue.put(("append", "assistant", resp))
                self.speak(resp)
                return

            if "date" in cmd:
                today = datetime.datetime.now().strftime("%A, %B %d, %Y")
                resp = f"Today is {today}."
                self.gui_queue.put(("append", "assistant", resp))
                self.speak(resp)
                return

            if "weather" in cmd:
                resp = self.get_weather()
                self.gui_queue.put(("append", "assistant", resp))
                self.speak(resp)
                return

            if "news" in cmd:
                resp = self.get_news()
                self.gui_queue.put(("append", "assistant", resp))
                self.speak("Here are the top headlines.")
                return

            if "joke" in cmd:
                resp = self.get_joke()
                self.gui_queue.put(("append", "assistant", resp))
                self.speak(resp)
                return

            if "play music" in cmd or "play song" in cmd:
                self.play_music_from_favorites()
                self.gui_queue.put(("append", "assistant", "Playing music from favorites."))
                return

            if "remind me" in cmd or "reminder" in cmd:
                resp = self.add_reminder(cmd)
                self.gui_queue.put(("append", "assistant", resp))
                self.speak(resp)
                return

            if "medication" in cmd or "pill" in cmd or "medicine" in cmd:
                resp = self.show_medication_schedule()
                self.gui_queue.put(("append", "assistant", resp))
                self.speak(resp)
                return

            if "open" in cmd and ("http" in cmd or "." in cmd or "website" in cmd):
                # crude open command
                target = command.replace("open", "").strip()
                if not target.startswith("http"):
                    if "website" in target:
                        target = target.replace("website", "").strip()
                    if not target.startswith("http"):
                        target = "https://" + target
                webbrowser.open(target)
                self.gui_queue.put(("append", "assistant", f"Opening {target}"))
                return

            if any(w in cmd for w in ("exit", "quit", "goodbye")):
                self.gui_queue.put(("append", "assistant", "Goodbye!"))
                self.speak("Goodbye!")
                self.after(800, self.on_close)
                return

            # no simple match — try OpenAI if configured
            openai_key = self.config_mgr.get("api_keys.openai", "")
            if openai_key:
                try:
                    openai.api_key = openai_key
                    # call ChatCompletion
                    resp_text = self.get_ai_response(command)
                    self.gui_queue.put(("append", "assistant", resp_text))
                    self.speak(resp_text)
                    return
                except Exception as e:
                    # fall through to wikipedia
                    print(f"[AI] error: {e}")

            # fallback to wikipedia short summary
            try:
                wiki_resp = wikipedia.summary(command, sentences=2, auto_suggest=False)
                self.gui_queue.put(("append", "assistant", wiki_resp))
                self.speak(wiki_resp)
                return
            except Exception as e:
                # final fallback
                self.gui_queue.put(("append", "assistant", "Sorry, I couldn't find an answer."))
                self.speak("Sorry, I couldn't find an answer to that.")
                return

        except Exception as e:
            self.gui_queue.put(("append", "assistant", f"An error occurred: {e}"))
            print(f"[Command] error: {e}")

    # -------------------------
    # OpenAI helper
    # -------------------------
    def get_ai_response(self, prompt: str) -> str:
        # Simple ChatCompletion usage
        try:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a concise, friendly assistant for an elderly user. Keep replies short and clear."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.6,
                max_tokens=200
            )
            text = response.choices[0].message.content.strip()
            return text
        except Exception as e:
            print(f"[OpenAI] error: {e}")
            raise

    # -------------------------
    # WEATHER / NEWS / JOKE
    # -------------------------
    def get_weather(self) -> str:
        api_key = self.config_mgr.get("api_keys.openweathermap", "")
        city = self.config.get("city", "New York")
        if not api_key:
            return "Weather API key not configured. Please add it in Settings."
        try:
            params = {"q": city, "appid": api_key, "units": "metric"}
            r = requests.get(WEATHER_URL, params=params, timeout=8)
            r.raise_for_status()
            data = r.json()
            temp = data["main"]["temp"]
            desc = data["weather"][0]["description"].capitalize()
            feels = data["main"].get("feels_like")
            return f"The weather in {city} is {desc}, {temp}°C (feels like {feels}°C)."
        except Exception as e:
            return f"Could not fetch weather: {e}"

    def get_news(self) -> str:
        api_key = self.config_mgr.get("api_keys.newsapi", "")
        if not api_key:
            return "News API key not configured. Please add it in Settings."
        try:
            params = {"country": "us", "apiKey": api_key, "pageSize": 3}
            r = requests.get(NEWS_URL, params=params, timeout=8)
            r.raise_for_status()
            articles = r.json().get("articles", [])
            if not articles:
                return "No headlines available right now."
            headlines = [f"{i+1}. {a.get('title','No title')}" for i, a in enumerate(articles)]
            return "Top headlines:\n" + "\n".join(headlines)
        except Exception as e:
            return f"Could not fetch news: {e}"

    def get_joke(self) -> str:
        jokes = [
            "Why don't scientists trust atoms? Because they make up everything.",
            "I told my wife she drew her eyebrows too high. She looked surprised.",
            "What do you call a fake noodle? An impasta!"
        ]
        return random.choice(jokes)

    # -------------------------
    # MUSIC
    # -------------------------
    def play_music_from_favorites(self):
        music_files = self.config_mgr.get("favorites.music", [])
        if not music_files:
            # open dialog to add music folder or files
            path = filedialog.askopenfilename(title="Select a music file", filetypes=[("Audio files", "*.mp3 *.wav *.ogg")])
            if path:
                music_files = [path]
                self.config_mgr.set("favorites.music", music_files)
                self.config_mgr.save()
            else:
                self.gui_queue.put(("append", "assistant", "No music files configured."))
                return
        try:
            choice = random.choice(music_files)
            mixer.music.load(choice)
            mixer.music.play()
            self.gui_queue.put(("append", "assistant", f"Now playing: {os.path.basename(choice)}"))
        except Exception as e:
            self.gui_queue.put(("append", "assistant", f"Could not play music: {e}"))

    # -------------------------
    # REMINDERS / MEDICATION
    # -------------------------
    def _schedule_medication_reminders(self):
        # cancel previously scheduled after jobs (best effort)
        for after_id in list(self._med_after_ids):
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self._med_after_ids.clear()

        schedule = self.config_mgr.get("medication_schedule", {}) or {}
        now = datetime.datetime.now()
        for med, times in schedule.items():
            for tstr in times:
                try:
                    hh, mm = [int(x) for x in tstr.strip().split(":")]
                    next_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if next_dt <= now:
                        next_dt += datetime.timedelta(days=1)
                    delta = (next_dt - now).total_seconds()
                    after_id = self.after(int(delta * 1000), lambda m=med, ts=tstr: self._trigger_medication(m, ts))
                    self._med_after_ids.append(after_id)
                except Exception as e:
                    print(f"[Schedule] invalid time {tstr} for {med}: {e}")

    def _trigger_medication(self, med: str, time_str: str):
        msg = f"It's time to take your {med} ({time_str})."
        self.gui_queue.put(("append", "assistant", msg))
        self.speak(msg, interrupt=True)
        # reschedule for next day
        # compute 24 hours in ms
        after_id = self.after(24 * 3600 * 1000, lambda: self._trigger_medication(med, time_str))
        self._med_after_ids.append(after_id)

    def show_medication_schedule(self):
        schedule = self.config_mgr.get("medication_schedule", {}) or {}
        if not schedule:
            self.gui_queue.put(("append", "assistant", "You don't have any medications scheduled. Add them in Settings."))
            return
        lines = ["Your medication schedule:"]
        for med, times in schedule.items():
            lines.append(f" - {med}: {', '.join(times)}")
        text = "\n".join(lines)
        self.gui_queue.put(("append", "assistant", text))
        return text

    def add_reminder(self, command_text: str) -> str:
        # Basic parsing for "remind me to <task> at HH:MM"
        if "remind me to" in command_text:
            part = command_text.split("remind me to", 1)[1].strip()
            # try to detect time at end
            words = part.rsplit(" at ", 1)
            if len(words) == 2:
                task, tstr = words[0].strip(), words[1].strip()
                try:
                    hh, mm = [int(x) for x in tstr.split(":")]
                    now = datetime.datetime.now()
                    remind_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if remind_dt <= now:
                        remind_dt += datetime.timedelta(days=1)
                    delta = (remind_dt - now).total_seconds()
                    self.after(int(delta * 1000), lambda: (self.gui_queue.put(("append", "assistant", f"Reminder: {task}")), self.speak(f"Reminder: {task}")))
                    return f"Okay — I'll remind you to {task} at {tstr}."
                except Exception:
                    return "I couldn't parse the time. Use HH:MM format."
            else:
                return f"Okay — I'll remind you to {part} (but no time was given)."
        return "Tell me what to remind you about using 'remind me to ... at HH:MM'."

    # -------------------------
    # CONTACTS / EMERGENCY
    # -------------------------
    def emergency_protocol(self):
        # Visual and audible alert + call simulation
        msg = "Emergency assistance requested! Contacting emergency contacts..."
        self.gui_queue.put(("append", "emergency", msg))
        self.speak("Emergency! Assistance requested!", interrupt=True)
        # simulate calls or alerts
        contacts = self.config_mgr.get("emergency_contacts", []) or []
        if not contacts:
            self.gui_queue.put(("append", "assistant", "No emergency contacts configured. Please add them in Settings."))
            return
        for c in contacts:
            name = c.get("name", "Unknown")
            phone = c.get("phone", "Unknown")
            self.gui_queue.put(("append", "system", f"Simulating call to {name} ({phone})..."))

    # -------------------------
    # SETTINGS UI
    # -------------------------
    def open_settings(self):
        SettingsDialog(self, self.config_mgr)

    def apply_settings_changes(self):
        # refresh config local pointer & UI changes
        self.config = self.config_mgr.data
        # update user label
        self.user_label.configure(text=f"Hello, {self.config.get('user_name','User')}")
        # theme
        try:
            ctk.set_appearance_mode(self.config.get("theme", "Dark"))
        except Exception:
            pass
        # font size
        new_font_size = int(self.config.get("font_size", 16))
        self.font_size = new_font_size
        self.textbox.configure(font=ctk.CTkFont(size=self.font_size))
        self.entry.configure(font=ctk.CTkFont(size=self.font_size))
        # tts
        try:
            tts_engine.setProperty("rate", int(self.config.get("voice_rate", 150)))
            tts_engine.setProperty("volume", float(self.config.get("voice_volume", 1.0)))
        except Exception:
            pass
        # update favorites etc
        # reschedule meds
        self._schedule_medication_reminders()
        # openai key
        openai_key = self.config_mgr.get("api_keys.openai", "")
        if openai_key:
            openai.api_key = openai_key

    # -------------------------
    # SHUTDOWN
    # -------------------------
    def on_close(self):
        # stop listening
        self.listening_event.clear()
        # stop TTS
        try:
            tts_engine.stop()
        except Exception:
            pass
        # stop mixer
        try:
            mixer.music.stop()
        except Exception:
            pass
        # exit
        self.destroy()
        try:
            sys.exit(0)
        except SystemExit:
            pass

# -------------------------
# SETTINGS DIALOG
# -------------------------
class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent: AURAApp, config_mgr: ConfigManager):
        super().__init__(parent)
        self.parent = parent
        self.config_mgr = config_mgr
        self.title("Settings")
        self.geometry("640x720")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._build_ui()
        self._load_values()

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(self, text="Settings", font=ctk.CTkFont(size=20, weight="bold"))
        title.grid(row=0, column=0, padx=16, pady=(12, 6), sticky="w")

        # Tabview
        self.tabview = ctk.CTkTabview(self, width=600)
        self.tabview.grid(row=1, column=0, padx=16, pady=8, sticky="nsew")
        for t in ("General", "API Keys", "Contacts", "Medication", "Favorites"):
            self.tabview.add(t)

        # General tab
        g = self.tabview.tab("General")
        g.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(g, text="User Name:").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        self.user_name_entry = ctk.CTkEntry(g)
        self.user_name_entry.grid(row=0, column=1, padx=8, pady=8, sticky="ew")

        ctk.CTkLabel(g, text="City (for weather):").grid(row=1, column=0, padx=8, pady=8, sticky="w")
        self.city_entry = ctk.CTkEntry(g)
        self.city_entry.grid(row=1, column=1, padx=8, pady=8, sticky="ew")

        ctk.CTkLabel(g, text="Theme:").grid(row=2, column=0, padx=8, pady=8, sticky="w")
        self.theme_var = ctk.StringVar(value=self.config_mgr.get("theme", "Dark"))
        self.theme_menu = ctk.CTkOptionMenu(g, values=["Dark", "Light", "System"], variable=self.theme_var)
        self.theme_menu.grid(row=2, column=1, padx=8, pady=8, sticky="w")

        ctk.CTkLabel(g, text="Font Size:").grid(row=3, column=0, padx=8, pady=8, sticky="w")
        self.font_slider = ctk.CTkSlider(g, from_=12, to=24, number_of_steps=12, command=self._update_font_label)
        self.font_slider.grid(row=3, column=1, padx=8, pady=8, sticky="ew")
        self.font_label = ctk.CTkLabel(g, text="")
        self.font_label.grid(row=3, column=2, padx=8, pady=8, sticky="w")

        ctk.CTkLabel(g, text="Voice Rate:").grid(row=4, column=0, padx=8, pady=8, sticky="w")
        self.rate_slider = ctk.CTkSlider(g, from_=80, to=240, number_of_steps=160, command=self._update_rate_label)
        self.rate_slider.grid(row=4, column=1, padx=8, pady=8, sticky="ew")
        self.rate_label = ctk.CTkLabel(g, text="")
        self.rate_label.grid(row=4, column=2, padx=8, pady=8, sticky="w")

        ctk.CTkLabel(g, text="Voice Volume:").grid(row=5, column=0, padx=8, pady=8, sticky="w")
        self.volume_slider = ctk.CTkSlider(g, from_=0.0, to=1.0, number_of_steps=10, command=self._update_volume_label)
        self.volume_slider.grid(row=5, column=1, padx=8, pady=8, sticky="ew")
        self.vol_label = ctk.CTkLabel(g, text="")
        self.vol_label.grid(row=5, column=2, padx=8, pady=8, sticky="w")

        # API Keys tab
        a = self.tabview.tab("API Keys")
        a.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(a, text="OpenWeatherMap Key:").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        self.weather_key_entry = ctk.CTkEntry(a, show="*")
        self.weather_key_entry.grid(row=0, column=1, padx=8, pady=8, sticky="ew")

        ctk.CTkLabel(a, text="NewsAPI Key:").grid(row=1, column=0, padx=8, pady=8, sticky="w")
        self.news_key_entry = ctk.CTkEntry(a, show="*")
        self.news_key_entry.grid(row=1, column=1, padx=8, pady=8, sticky="ew")

        ctk.CTkLabel(a, text="OpenAI Key:").grid(row=2, column=0, padx=8, pady=8, sticky="w")
        self.openai_key_entry = ctk.CTkEntry(a, show="*")
        self.openai_key_entry.grid(row=2, column=1, padx=8, pady=8, sticky="ew")

        # Contacts tab
        ctab = self.tabview.tab("Contacts")
        ctab.grid_rowconfigure(0, weight=1)
        ctab.grid_columnconfigure(0, weight=1)
        self.contacts_frame = ctk.CTkScrollableFrame(ctab, label_text="Emergency Contacts")
        self.contacts_frame.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        self.contact_widgets = []  # list of dicts {frame, name_entry, relation_entry, phone_entry}
        add_contact_btn = ctk.CTkButton(ctab, text="+ Add Contact", command=self._add_contact_widget)
        add_contact_btn.grid(row=1, column=0, padx=8, pady=8, sticky="ew")

        # Medication tab
        mtab = self.tabview.tab("Medication")
        mtab.grid_rowconfigure(0, weight=1)
        mtab.grid_columnconfigure(0, weight=1)
        self.med_frame = ctk.CTkScrollableFrame(mtab, label_text="Medication Schedule (HH:MM comma-separated)")
        self.med_frame.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        self.med_widgets = []
        add_med_btn = ctk.CTkButton(mtab, text="+ Add Medication", command=self._add_med_widget)
        add_med_btn.grid(row=1, column=0, padx=8, pady=8, sticky="ew")

        # Favorites tab
        ftab = self.tabview.tab("Favorites")
        ftab.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(ftab, text="Favorite Music Files (one per line)").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        self.music_text = ctk.CTkTextbox(ftab, height=160)
        self.music_text.grid(row=1, column=0, padx=8, pady=8, sticky="ew")
        add_music_btn = ctk.CTkButton(ftab, text="Add Files...", command=self._add_music_files)
        add_music_btn.grid(row=2, column=0, padx=8, pady=8, sticky="w")

        # Save/Cancel
        button_frame = ctk.CTkFrame(self)
        button_frame.grid(row=2, column=0, padx=16, pady=12, sticky="ew")
        button_frame.grid_columnconfigure((0,1), weight=1)
        save_btn = ctk.CTkButton(button_frame, text="Save & Apply", command=self.save_settings)
        save_btn.grid(row=0, column=0, padx=6, sticky="ew")
        cancel_btn = ctk.CTkButton(button_frame, text="Cancel", fg_color="#D35B58", hover_color="#a34643", command=self.cancel)
        cancel_btn.grid(row=0, column=1, padx=6, sticky="ew")

    def _update_font_label(self, v):
        self.font_label.configure(text=str(int(float(v))))

    def _update_rate_label(self, v):
        self.rate_label.configure(text=str(int(float(v))))

    def _update_volume_label(self, v):
        self.vol_label.configure(text=f"{float(v):.2f}")

    def _load_values(self):
        cfg = self.config_mgr.data
        # General
        self.user_name_entry.insert(0, cfg.get("user_name", "User"))
        self.city_entry.insert(0, cfg.get("city", "New York"))
        self.theme_var.set(cfg.get("theme", "Dark"))
        self.font_slider.set(cfg.get("font_size", 16))
        self._update_font_label(self.font_slider.get())
        self.rate_slider.set(cfg.get("voice_rate", 150))
        self._update_rate_label(self.rate_slider.get())
        self.volume_slider.set(cfg.get("voice_volume", 1.0))
        self._update_volume_label(self.volume_slider.get())

        # API Keys
        apis = cfg.get("api_keys", {})
        self.weather_key_entry.insert(0, apis.get("openweathermap", ""))
        self.news_key_entry.insert(0, apis.get("newsapi", ""))
        self.openai_key_entry.insert(0, apis.get("openai", ""))

        # Contacts
        for c in cfg.get("emergency_contacts", []):
            self._add_contact_widget(c)

        # Medication
        for med, times in cfg.get("medication_schedule", {}).items():
            self._add_med_widget(med, ", ".join(times))

        # Favorites
        music = cfg.get("favorites", {}).get("music", [])
        self.music_text.insert("1.0", "\n".join(music))

    def _add_contact_widget(self, contact: Optional[Dict] = None):
        frame = ctk.CTkFrame(self.contacts_frame)
        frame.grid(padx=6, pady=6, sticky="ew")
        frame.grid_columnconfigure((0,1,2), weight=1)
        name_entry = ctk.CTkEntry(frame, placeholder_text="Name")
        name_entry.grid(row=0, column=0, padx=6, pady=6, sticky="ew")
        rel_entry = ctk.CTkEntry(frame, placeholder_text="Relation")
        rel_entry.grid(row=0, column=1, padx=6, pady=6, sticky="ew")
        phone_entry = ctk.CTkEntry(frame, placeholder_text="Phone")
        phone_entry.grid(row=0, column=2, padx=6, pady=6, sticky="ew")
        remove = ctk.CTkButton(frame, text="×", width=36, command=lambda f=frame: self._remove_contact_widget(f))
        remove.grid(row=0, column=3, padx=6, pady=6)
        if contact:
            name_entry.insert(0, contact.get("name", ""))
            rel_entry.insert(0, contact.get("relation", ""))
            phone_entry.insert(0, contact.get("phone", ""))
        self.contact_widgets.append({"frame": frame, "name": name_entry, "relation": rel_entry, "phone": phone_entry})

    def _remove_contact_widget(self, frame):
        for i, w in enumerate(self.contact_widgets):
            if w["frame"] == frame:
                w["frame"].destroy()
                self.contact_widgets.pop(i)
                return

    def _add_med_widget(self, name: Optional[str] = None, times: Optional[str] = None):
        frame = ctk.CTkFrame(self.med_frame)
        frame.grid(padx=6, pady=6, sticky="ew")
        frame.grid_columnconfigure(0, weight=1)
        name_entry = ctk.CTkEntry(frame, placeholder_text="Medication Name")
        name_entry.grid(row=0, column=0, padx=6, pady=6, sticky="ew")
        times_entry = ctk.CTkEntry(frame, placeholder_text="Times (HH:MM, comma-separated)")
        times_entry.grid(row=1, column=0, padx=6, pady=6, sticky="ew")
        remove = ctk.CTkButton(frame, text="×", width=36, command=lambda f=frame: self._remove_med_widget(f))
        remove.grid(row=0, column=1, rowspan=2, padx=6, pady=6)
        if name:
            name_entry.insert(0, name)
        if times:
            times_entry.insert(0, times)
        self.med_widgets.append({"frame": frame, "name": name_entry, "times": times_entry})

    def _remove_med_widget(self, frame):
        for i, w in enumerate(self.med_widgets):
            if w["frame"] == frame:
                w["frame"].destroy()
                self.med_widgets.pop(i)
                return

    def _add_music_files(self):
        files = filedialog.askopenfilenames(title="Select music files", filetypes=[("Audio", "*.mp3 *.wav *.ogg")])
        if files:
            cur = set(line.strip() for line in self.music_text.get("1.0", "end-1c").splitlines() if line.strip())
            cur.update(files)
            self.music_text.delete("1.0", "end")
            self.music_text.insert("1.0", "\n".join(sorted(cur)))

    def save_settings(self):
        try:
            # General
            self.config_mgr.set("user_name", self.user_name_entry.get().strip() or DEFAULT_CONFIG["user_name"])
            self.config_mgr.set("city", self.city_entry.get().strip() or DEFAULT_CONFIG["city"])
            self.config_mgr.set("theme", self.theme_var.get() or DEFAULT_CONFIG["theme"])
            self.config_mgr.set("font_size", int(float(self.font_slider.get())))
            self.config_mgr.set("voice_rate", int(float(self.rate_slider.get())))
            self.config_mgr.set("voice_volume", float(self.volume_slider.get()))

            # Api keys
            self.config_mgr.set("api_keys.openweathermap", self.weather_key_entry.get().strip())
            self.config_mgr.set("api_keys.newsapi", self.news_key_entry.get().strip())
            self.config_mgr.set("api_keys.openai", self.openai_key_entry.get().strip())

            # Contacts
            contacts = []
            for w in self.contact_widgets:
                name = w["name"].get().strip()
                phone = w["phone"].get().strip()
                relation = w["relation"].get().strip()
                if name and phone:
                    contacts.append({"name": name, "phone": phone, "relation": relation})
            self.config_mgr.set("emergency_contacts", contacts)

            # Medication
            meds = {}
            for w in self.med_widgets:
                name = w["name"].get().strip()
                times_raw = w["times"].get().strip()
                if name and times_raw:
                    times = [t.strip() for t in times_raw.split(",") if t.strip()]
                    meds[name] = times
            self.config_mgr.set("medication_schedule", meds)

            # Favorites
            music_lines = [line.strip() for line in self.music_text.get("1.0", "end-1c").splitlines() if line.strip()]
            self.config_mgr.set("favorites.music", music_lines)

            # Save to file
            ok = self.config_mgr.save()
            if not ok:
                raise RuntimeError("Failed to write config file.")

            # Apply changes to parent
            self.parent.apply_settings_changes()

            messagebox.showinfo("Settings", "Settings saved and applied.")
            self.destroy()
        except Exception as e:
            messagebox.showerror("Error saving settings", str(e), parent=self)

    def cancel(self):
        self.destroy()

# -------------------------
# RUN
# -------------------------
def main():
    app = AURAApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()

if __name__ == "__main__":
    main()
