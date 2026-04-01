import os
import threading
import re
from pathlib import Path
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
import anthropic

app = Flask(__name__)

# Clients
slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
verifier = SignatureVerifier(os.environ["SLACK_SIGNING_SECRET"])
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Load system prompt from file
SYSTEM_PROMPT = Path("system_prompt.txt").read_text()

# Track processed event IDs to avoid duplicate replies
processed_events = set()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/slack/events", methods=["POST"])
def slack_events():
    # Verify request is from Slack
    if not verifier.is_valid_request(request.get_data(), request.headers):
        return jsonify({"error": "Invalid request signature"}), 403

    data = request.json

    # Handle Slack URL verification challenge (one-time setup)
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    # Handle event callbacks
    if data.get("type") == "event_callback":
        event = data.get("event", {})
        event_id = data.get("event_id", "")

        # Deduplicate (Slack can retry events)
        if event_id in processed_events:
            return jsonify({"ok": True})
        processed_events.add(event_id)
        # Keep set from growing unbounded
        if len(processed_events) > 1000:
            processed_events.clear()

        if event.get("type") == "app_mention":
            # Don't respond to bot's own messages
            if event.get("bot_id"):
                return jsonify({"ok": True})

            # Process in background thread so we respond to Slack within 3s
            threading.Thread(target=handle_mention, args=(event,), daemon=True).start()

    return jsonify({"ok": True})


def handle_mention(event):
    channel = event["channel"]
    thread_ts = event.get("thread_ts", event["ts"])
    raw_text = event.get("text", "")

    # Strip the @bot mention from the message
    question = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()

    if not question:
        question = "Hello! What can I help you with?"

    # Build conversation — fetch thread history for context if it's a threaded reply
    messages = []
    if event.get("thread_ts") and event["thread_ts"] != event["ts"]:
        try:
            history = slack_client.conversations_replies(
                channel=channel,
                ts=event["thread_ts"],
                limit=10
            )
            for msg in history.get("messages", [])[:-1]:  # exclude the triggering message
                if msg.get("bot_id"):
                    messages.append({"role": "assistant", "content": msg.get("text", "")})
                elif msg.get("text"):
                    clean = re.sub(r"<@[A-Z0-9]+>", "", msg["text"]).strip()
                    if clean:
                        messages.append({"role": "user", "content": clean})
        except Exception:
            pass  # If thread fetch fails, continue without history

    # Add the current question
    messages.append({"role": "user", "content": question})

    # Call Claude
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        answer = response.content[0].text
    except Exception as e:
        answer = f"⚠️ Error reaching Claude: {str(e)}"

    # Post reply in thread
    try:
        slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=answer,
            mrkdwn=True,
        )
    except Exception as e:
        print(f"Failed to post to Slack: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
