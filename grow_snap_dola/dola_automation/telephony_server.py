import sys
import os
import time
import socket
import threading
import json
import sqlite3
import urllib.request
import urllib.error
from pathlib import Path

# ─── CONFIGURATION & LOGGING ──────────────────────────────────────────────────

PORT = 5050
OLLAMA_URL = "http://localhost:11434/api/generate"
DB_PATH = Path.home() / 'Documents' / 'dola_video_automation' / 'history.db'

def log_message(msg: str):
    t_stamp = time.strftime("%H:%M:%S")
    print(f"[{t_stamp}] {msg}", flush=True)

# ─── DATABASE OPERATIONS ──────────────────────────────────────────────────────

def log_call_history(phone: str, transcript: str):
    """Save the full call transcript and summarize memory context."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        # Log communication log
        cursor.execute(
            "INSERT INTO communication_history (phone_number, channel, message, status) VALUES (?, ?, ?, ?)",
            (phone, "voice", f"Live Call Transcript:\n{transcript}", "sent")
        )
        
        # Summarize memory context
        memory_summary = f"Completed local AI voice outreach call with contact. Conversation history: {transcript[:200]}..."
        cursor.execute(
            "INSERT OR REPLACE INTO contact_memory (phone_number, summary) VALUES (?, ?)",
            (phone, memory_summary)
        )
        
        conn.commit()
        conn.close()
        log_message(f"Successfully saved call transcript to db history for {phone}.")
    except Exception as e:
        log_message(f"[Database Error] Failed saving call: {e}")

# ─── AI OLLAMA INFERENCE ──────────────────────────────────────────────────────

def query_ollama_ai(prompt: str, context: str) -> str:
    """Send voice transcript input to local Ollama instance and fetch response text."""
    system_prompt = (
        "You are an outreach assistant for GrowSnap AI. Be friendly, concise, and helpful. "
        "Your responses will be read via Text-to-Speech (TTS), so do not use markdown symbols or bullets."
    )
    payload = {
        "model": "llama3",
        "prompt": f"{system_prompt}\n\nClient Transcript: {prompt}\n\nAI Agent Response:",
        "stream": False
    }
    
    try:
        req = urllib.request.Request(
            OLLAMA_URL, 
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            resp_data = json.loads(response.read().decode('utf-8'))
            return resp_data.get("response", "I understand. How else can I assist you today?")
    except urllib.error.URLError as url_err:
        log_message(f"[Warning] Ollama server unreachable at {OLLAMA_URL}. (Using local simulation response).")
        return "I received your message. Let me sync this with our dashboard settings."
    except Exception as e:
        log_message(f"[Error] Ollama query failed: {e}")
        return "I understand. Let me verify that."

# ─── SPEECH TRANSCRIPTION & SYNTHESIS (SIMULATOR) ─────────────────────────────

def process_incoming_audio_to_text(audio_chunk: bytes) -> str:
    """ASR Simulation - In production, this pipes into faster-whisper."""
    # Simulating STT transcription of incoming audio stream
    return "Hello, I am interested in scheduling a towing pickup request."

def process_text_to_outbound_speech(text: str) -> bytes:
    """TTS Simulation - In production, this pipes into OuteTTS."""
    # Synthesizing speaking response
    return b"AudioWaveBytes"

# ─── CONNECTION HANDLING ──────────────────────────────────────────────────────

def handle_client_stream(client_socket: socket.socket, addr: tuple):
    log_message(f"New audio connection from: {addr[0]}:{addr[1]}")
    phone_number = "+1000000000"
    full_transcript = []
    
    # 1. Send opening audio greeting
    greeting = "Hello! Thank you for calling GrowSnap AI. How can I help you today?"
    full_transcript.append(f"Agent: {greeting}")
    log_message(f"Agent: '{greeting}'")
    
    try:
        # Mocking audio stream interaction
        client_socket.sendall(process_text_to_outbound_speech(greeting))
        
        while True:
            data = client_socket.recv(1024)
            if not data:
                break
            
            # 2. Transcribe incoming customer speech
            user_text = process_incoming_audio_to_text(data)
            full_transcript.append(f"User: {user_text}")
            log_message(f"User: '{user_text}'")
            
            # 3. Fetch response from Ollama
            ai_response = query_ollama_ai(user_text, "".join(full_transcript))
            full_transcript.append(f"Agent: {ai_response}")
            log_message(f"Agent: '{ai_response}'")
            
            # 4. Stream synthesized TTS audio back to customer
            audio_response = process_text_to_outbound_speech(ai_response)
            client_socket.sendall(audio_response)
            
            # Simulating call completion to prevent infinite loop in simulation
            break
            
    except Exception as e:
        log_message(f"Error handling connection chunk: {e}")
    finally:
        client_socket.close()
        log_message(f"Connection closed for: {addr[0]}:{addr[1]}")
        
        # 5. Persist transcript records in SQLite database
        log_call_history(phone_number, "\n".join(full_transcript))

# ─── SERVER INGESTION CORE ────────────────────────────────────────────────────

def run_telephony_server():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Allow port reuse
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        server_socket.bind(("0.0.0.0", PORT))
        server_socket.listen(5)
        log_message(f"=== Telephony Server active and listening on port {PORT} ===")
        log_message("Waiting for inbound audio streams...")
        
        while True:
            client_sock, addr = server_socket.accept()
            client_thread = threading.Thread(target=handle_client_stream, args=(client_sock, addr), daemon=True)
            client_thread.start()
    except KeyboardInterrupt:
        log_message("Shutting down Telephony Server...")
    except Exception as e:
        log_message(f"[Fatal] Server socket error: {e}")
    finally:
        server_socket.close()

if __name__ == "__main__":
    # Handle optional port override
    if len(sys.argv) > 1:
        try:
            PORT = int(sys.argv[1])
        except ValueError:
            pass
            
    # Handle optional Ollama URL override
    if len(sys.argv) > 2:
        OLLAMA_URL = sys.argv[2]
        
    run_telephony_server()
