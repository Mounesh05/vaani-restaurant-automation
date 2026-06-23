# VAANI 🎙️

### Voice AI Restaurant Automation System

End-to-end AI assistant for restaurant operations. Handles table reservations through natural conversation, answers menu/policy questions using RAG, sends WhatsApp confirmations with QR codes, and provides admin dashboard for booking management.

![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.1-000000?logo=flask&logoColor=white)
![ChromaDB](https://img.shields.io/badge/Vector%20DB-ChromaDB-FF6F00)
![Ollama](https://img.shields.io/badge/LLM-Ollama-111111)
![SQLite](https://img.shields.io/badge/Database-SQLite-003B57?logo=sqlite)

---

## ✨ Key Features

- 🎙️ **Voice-First Interface** — Natural conversation for bookings and cancellations
- 🧠 **Intent Recognition** — Slot extraction pipeline for structured reservation handling
- 📚 **RAG Knowledge Base** — ChromaDB + nomic-embed-text for menu/FAQ retrieval from PDF/TXT
- 💬 **WhatsApp Integration** — Automated confirmations/cancellations via Twilio with QR codes
- 🛡️ **Production Security** — CSRF protection, rate limiting, brute-force lockout, secure sessions
- 📊 **Admin Dashboard** — Booking management with filters, pagination, CSV export

---

## 🧠 How It Works

1. Customer interacts via web chat/voice interface
2. VAANI classifies intent (book, cancel, menu, hours, FAQ)
3. For bookings/cancellations, required slots collected step-by-step
4. Data stored in SQLite; QR code generated for verification
5. WhatsApp messages sent to customer and optionally owner
6. For knowledge questions, RAG retrieves context from local documents
7. Admin manages bookings through dashboard

---

## 🏗️ Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Flask, Werkzeug |
| **AI/ML** | Ollama (llama3.2), ChromaDB, sentence-transformers, nomic-embed-text |
| **Database** | SQLite with WAL mode |
| **Messaging** | Twilio WhatsApp API |
| **Voice** | Edge-TTS |
| **Security** | Flask-WTF (CSRF), Flask-Limiter |

---

## 📁 Project Structure

```
vaani-restaurant-automation/
├── app.py                  # Flask app, routes, security, admin APIs
├── brain.py                # Conversation logic: intent, slots, RAG answers
├── database.py             # SQLite operations: CRUD, search, export
├── rag_store.py            # ChromaDB indexing + retrieval pipeline
├── whatsapp.py             # Twilio WhatsApp notifications
├── qr_tool.py              # QR code generation
├── config.py               # Environment-based configuration
├── requirements.txt        # Python dependencies
├── .env.example            # Environment template (safe to commit)
├── knowledge/              # Restaurant docs (.pdf/.txt) — add yours here
│   └── AM_Restaurant_Knowledge.pdf
├── templates/              # HTML templates
│   ├── index.html         # Customer interface
│   ├── admin.html         # Admin dashboard
│   └── admin_login.html   # Admin authentication
├── static/                 # Static assets
│   ├── css/
│   ├── js/
│   └── qr/                # Generated QR codes (gitignored)
├── chroma_db/             # Vector store (auto-generated, gitignored)
└── bookings.db            # SQLite database (gitignored)
```

---

## 🚀 Quick Start

### Prerequisites

- Python 3.9+
- Ollama installed and running
- Twilio account (optional, for WhatsApp features)
- ngrok (optional, for local dev with WhatsApp)

### 1. Clone repository

```bash
git clone https://github.com/Mounesh05/vaani-restaurant-automation.git
cd vaani-restaurant-automation
```

### 2. Create virtual environment

```bash
python -m venv venv

# Activate
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Setup Ollama

```bash
# Download from https://ollama.ai
# Pull required models
ollama pull llama3.2:3b
ollama pull nomic-embed-text

# Start Ollama server
ollama serve
```

### 5. Configure environment

```bash
# Copy template
cp .env.example .env

# Edit .env with your values
# Generate Flask secret key:
python -c "import secrets; print(secrets.token_hex(32))"
```

**Minimum required in `.env`:**

```env
FLASK_SECRET_KEY=your_64_char_secret_key_here
ADMIN_PIN=your_admin_pin
OLLAMA_URL=http://localhost:11434/api/generate
OLLAMA_MODEL=llama3.2:3b
```

### 6. Initialize RAG knowledge base

```bash
# Ensure knowledge docs are in knowledge/ folder
# Build vector index
python rag_store.py
```

### 7. Run application

```bash
python app.py
```

**Access points:**
- Customer interface: `http://localhost:5000`
- Admin dashboard: `http://localhost:5000/admin`
- Health check: `http://localhost:5000/health`

---

## 🔧 Configuration

### Core Variables (Required)

| Variable | Description | Example |
|----------|-------------|---------|
| `FLASK_SECRET_KEY` | Session encryption key (≥32 chars) | Generate with `secrets.token_hex(32)` |
| `ADMIN_PIN` | Admin dashboard access PIN | `123456` |
| `OLLAMA_URL` | Ollama generate endpoint | `http://localhost:11434/api/generate` |
| `OLLAMA_MODEL` | Chat model for conversations | `llama3.2:3b` |

### Database & Capacity

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `bookings.db` | SQLite database file path |
| `MAX_RESTAURANT_CAPACITY` | `5` | Max confirmed bookings per time slot |

### RAG Tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `RAG_MAX_DISTANCE` | `0.55` | Max cosine distance for retrieval (lower = stricter) |
| `RAG_MIN_LEXICAL_OVERLAP` | `0.08` | Min lexical overlap threshold |

### WhatsApp Integration (Optional)

| Variable | Purpose |
|----------|---------|
| `TWILIO_SID` | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Twilio auth token |
| `TWILIO_WHATSAPP_NUMBER` | Twilio WhatsApp sender number |
| `OWNER_WHATSAPP` | Restaurant owner number (receives copies) |
| `NGROK_URL` | Public URL for QR code delivery in messages |

### Other Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | Flask server port |
| `FLASK_ENV` | `development` | Set `production` for secure cookies |
| `FLASK_DEBUG` | `false` | Enable debug mode (dev only) |

---

## 🧭 API Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Customer voice interface |
| `/send_message` | POST | Main AI conversation endpoint |
| `/reset_state` | POST | Clear conversation state |
| `/admin/login` | GET/POST | Admin authentication |
| `/admin` | GET | Admin dashboard with filters |
| `/admin/cancel/<id>` | POST | Cancel booking (CSRF protected) |
| `/admin/restore/<id>` | POST | Restore cancelled booking |
| `/admin/export` | GET | Export bookings as CSV |
| `/health` | GET | Service health check |

---

## 🎤 Using VAANI

### Customer Interface

1. Visit homepage
2. Click **"Start Call"**
3. Speak or type naturally:
   - "Book a table for 4 people tomorrow at 7 PM"
   - "What's on the menu?"
   - "Cancel my booking for June 15th"
   - "What are your opening hours?"

### Admin Dashboard

1. Navigate to `/admin/login`
2. Enter admin PIN
3. Features:
   - View all bookings with real-time stats
   - Filter by name/phone/date/status
   - Cancel or restore bookings
   - Export filtered results to CSV
   - Pagination support

---

## 🛡️ Security Features

- ✅ **CSRF Protection** — All state-changing operations require valid tokens
- ✅ **Rate Limiting** — 300 requests/hour, 60/minute per IP
- ✅ **Brute-Force Protection** — Admin login lockout after 5 failed attempts (5-min timeout)
- ✅ **Secure Sessions** — HTTPOnly, SameSite, Secure flags in production
- ✅ **Security Headers** — X-Content-Type-Options, X-Frame-Options, X-XSS-Protection
- ✅ **Input Validation** — Length caps, type validation, SQL injection prevention

---

## 🐛 Troubleshooting

### Ollama Connection Error

```bash
# Check if Ollama is running
ollama serve

# Verify models are pulled
ollama list

# Pull missing models
ollama pull llama3.2:3b
ollama pull nomic-embed-text
```

### RAG Returns No Results / Weak Answers

```bash
# Verify knowledge files exist
ls knowledge/

# Rebuild vector index
python rag_store.py
```

### WhatsApp Messages Not Sending

- Verify Twilio credentials in `.env`
- Check `NGROK_URL` is set and reachable
- Ensure phone numbers include country code (e.g., `+919876543210`)
- Confirm ngrok is running if testing locally

### Database Locked Error

```bash
# SQLite WAL mode should prevent this
# If it occurs, check no other process is accessing bookings.db
# Restart the app
```

---

## 📝 Notes

- **Large Groups**: Bookings above 10 guests redirected to private dining contact
- **First Run**: If `chroma_db/` missing, app attempts to build index on startup
- **Time Zones**: All times stored in `Asia/Kolkata` by default (configurable in `config.py`)
- **QR Codes**: Generated in `static/qr/` — automatically cleaned up via gitignore

---

## 👤 Authors

**Mounesh S D**  
- Undergraduate AI/ML Student, Dr. AIT Bengaluru (Batch 2023-2027)
**Ayush A Waster**
  - Undergraduate AI/ML Student, Dr. AIT Bengaluru (Batch 2023-2027)
**Project Context**: Academic research project demonstrating AI-powered restaurant automation with RAG, NLP, and real-time integrations
---

## 📄 License

This project developed for academic/research purposes. See `LICENSE` file for details.

---

## 🙏 Acknowledgments

- **Flask** ecosystem for rapid web development
- **Ollama** and **ChromaDB** for local-first AI retrieval
- **Twilio** for WhatsApp Business API integration
- **Edge-TTS** for natural voice synthesis
- **sentence-transformers** for semantic embeddings

---

## 🔗 Quick Links

- [Ollama Documentation](https://ollama.ai/docs)
- [ChromaDB Documentation](https://docs.trychroma.com)
- [Twilio WhatsApp API](https://www.twilio.com/docs/whatsapp)
- [Flask Documentation](https://flask.palletsprojects.com)

---

**Built with ❤️ for seamless restaurant operations**
