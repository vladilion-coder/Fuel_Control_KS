# server.py — веб-обгортка для Telegram webhook на Render
import os
import json
from flask import Flask, request, abort
from telegram import Update
from telegram.ext import Application
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
assert BOT_TOKEN, "BOT_TOKEN is not set"

# Імпортуємо вже налаштований Application з bot.py
# Якщо у твоєму bot.py немає фабрики, додай туди функцію build_app() яка повертає Application без run_polling()
from bot import build_app  # ← ти маєш мати таку функцію (див. примітку нижче)

app = Flask(__name__)
application = build_app(BOT_TOKEN)  # створюємо Application один раз (глобально)

@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def tg_webhook():
    if request.headers.get("content-type") == "application/json":
        update = Update.de_json(request.get_json(force=True), application.bot)
        # Передаємо апдейти в PTB
        application.update_queue.put_nowait(update)
        return "OK", 200
    else:
        abort(403)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
