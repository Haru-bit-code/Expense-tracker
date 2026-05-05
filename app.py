from flask import Flask, request, jsonify, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    JWTManager, create_access_token,
    jwt_required, get_jwt_identity
)
from datetime import timedelta, datetime, date, timezone
import os, io, json
from dotenv import load_dotenv
from PIL import Image
import base64, requests as http_requests

load_dotenv()

_GROQ_KEY  = os.getenv("GROQ_API_KEY", "")
_GROQ_URL  = "https://api.groq.com/openai/v1/chat/completions"

def _groq(messages, model="llama-3.3-70b-versatile", max_tokens=512):
    resp = http_requests.post(
        _GROQ_URL,
        headers={"Authorization": f"Bearer {_GROQ_KEY}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "max_tokens": max_tokens},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────
app.config["SQLALCHEMY_DATABASE_URI"]      = "sqlite:///expenses.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["JWT_SECRET_KEY"]               = "super-secret-jwt-key-2024"
app.config["JWT_ACCESS_TOKEN_EXPIRES"]     = timedelta(hours=24)

db     = SQLAlchemy(app)
bcrypt = Bcrypt(app)
jwt    = JWTManager(app)

# ── Models ────────────────────────────────────────────────────

class User(db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80),  unique=True, nullable=False)
    email    = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    expenses = db.relationship("Expense", backref="owner", lazy=True)

class Expense(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(100), nullable=False)
    amount      = db.Column(db.Float,       nullable=False)
    category    = db.Column(db.String(50),  nullable=False)
    description = db.Column(db.String(200), default="")
    date        = db.Column(db.String(20),  nullable=False)
    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    def to_dict(self):
        return {
            "id":          self.id,
            "title":       self.title,
            "amount":      self.amount,
            "category":    self.category,
            "description": self.description,
            "date":        self.date
        }

# ── Auth Routes ───────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/register", methods=["POST"])
def register():
    data     = request.get_json()
    username = data.get("username", "").strip()
    email    = data.get("email",    "").strip()
    password = data.get("password", "").strip()

    if not username or not email or not password:
        return jsonify({"error": "All fields are required"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Email already registered"}), 409

    if User.query.filter_by(username=username).first():
        return jsonify({"error": "Username already taken"}), 409

    hashed = bcrypt.generate_password_hash(password).decode("utf-8")
    user   = User(username=username, email=email, password=hashed)
    db.session.add(user)
    db.session.commit()

    token = create_access_token(identity=str(user.id))
    return jsonify({"token": token, "username": username}), 201

@app.route("/api/login", methods=["POST"])
def login():
    data     = request.get_json()
    email    = data.get("email",    "").strip()
    password = data.get("password", "").strip()

    user = User.query.filter_by(email=email).first()

    if not user or not bcrypt.check_password_hash(user.password, password):
        return jsonify({"error": "Invalid email or password"}), 401

    token = create_access_token(identity=str(user.id))
    return jsonify({"token": token, "username": user.username}), 200

# ── Expense Routes (Protected) ────────────────────────────────

@app.route("/api/expenses", methods=["GET"])
@jwt_required()
def get_expenses():
    user_id  = int(get_jwt_identity())
    category = request.args.get("category")
    query    = Expense.query.filter_by(user_id=user_id)

    if category and category != "All":
        query = query.filter_by(category=category)

    expenses = query.order_by(Expense.date.desc()).all()
    total    = sum(e.amount for e in Expense.query.filter_by(user_id=user_id).all())

    return jsonify({
        "expenses": [e.to_dict() for e in expenses],
        "total":    round(total, 2)
    })

@app.route("/api/expenses", methods=["POST"])
@jwt_required()
def add_expense():
    user_id = int(get_jwt_identity())
    data    = request.get_json()

    if not data.get("title") or not data.get("amount") or not data.get("date"):
        return jsonify({"error": "Title, amount and date are required"}), 400

    expense = Expense(
        title       = data["title"].strip(),
        amount      = float(data["amount"]),
        category    = data.get("category", "Other"),
        description = data.get("description", ""),
        date        = data["date"],
        user_id     = user_id
    )
    db.session.add(expense)
    db.session.commit()
    return jsonify(expense.to_dict()), 201

@app.route("/api/expenses/<int:expense_id>", methods=["PUT"])
@jwt_required()
def update_expense(expense_id):
    user_id = int(get_jwt_identity())
    expense = Expense.query.filter_by(id=expense_id, user_id=user_id).first()

    if not expense:
        return jsonify({"error": "Expense not found"}), 404

    data = request.get_json()
    if "title"       in data: expense.title       = data["title"].strip()
    if "amount"      in data: expense.amount      = float(data["amount"])
    if "category"    in data: expense.category    = data["category"]
    if "description" in data: expense.description = data["description"]
    if "date"        in data: expense.date        = data["date"]

    db.session.commit()
    return jsonify(expense.to_dict())

@app.route("/api/expenses/<int:expense_id>", methods=["DELETE"])
@jwt_required()
def delete_expense(expense_id):
    user_id = int(get_jwt_identity())
    expense = Expense.query.filter_by(id=expense_id, user_id=user_id).first()

    if not expense:
        return jsonify({"error": "Expense not found"}), 404

    db.session.delete(expense)
    db.session.commit()
    return jsonify({"message": "Deleted successfully"})

@app.route("/api/summary", methods=["GET"])
@jwt_required()
def summary():
    user_id  = int(get_jwt_identity())
    expenses = Expense.query.filter_by(user_id=user_id).all()

    by_category = {}
    for e in expenses:
        by_category[e.category] = round(
            by_category.get(e.category, 0) + e.amount, 2
        )

    return jsonify({
        "total":       round(sum(e.amount for e in expenses), 2),
        "count":       len(expenses),
        "by_category": by_category
    })

# ── Receipt Routes ────────────────────────────────────────────

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

def _allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/api/receipt/scan", methods=["POST"])
@jwt_required()
def receipt_scan():
    if "receipt" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["receipt"]
    if not _allowed(f.filename):
        return jsonify({"error": "Only JPG, PNG, WEBP images are accepted"}), 400

    try:
        img = Image.open(f.stream).convert("RGB")
        max_side = 1600
        if max(img.size) > max_side:
            ratio = max_side / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        image_bytes = buf.getvalue()
    except Exception:
        return jsonify({"error": "Could not process image"}), 400

    prompt = (
        "You are a receipt parser. Extract only individual purchasable line items from this receipt image.\n"
        "Return ONLY a valid JSON array with no markdown, no explanation, no code fences.\n"
        'Format: [{"item": "Product Name", "price": 99.00}, ...]\n'
        "Rules:\n"
        "- price must be a plain number (no currency symbols)\n"
        "- Skip rows that are totals, subtotals, taxes, discounts, or store info\n"
        "- If price is unclear, use 0.00\n"
        "- Item names should be clean and readable"
    )

    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        text = _groq(
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]
            }],
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            max_tokens=1024,
        )
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        items = json.loads(text.strip())
        if not isinstance(items, list):
            raise ValueError("Expected a list")
    except json.JSONDecodeError:
        return jsonify({"error": "Could not parse items from receipt"}), 422
    except Exception as e:
        return jsonify({"error": f"AI processing failed: {str(e)}"}), 500

    return jsonify({"items": items})


@app.route("/api/receipt/confirm", methods=["POST"])
@jwt_required()
def receipt_confirm():
    user_id = int(get_jwt_identity())
    data    = request.get_json()
    items   = data.get("items", [])

    if not items:
        return jsonify({"error": "No items provided"}), 400

    today    = date.today().isoformat()
    expenses = []
    for it in items:
        name  = str(it.get("item", "Unknown")).strip() or "Unknown"
        price = float(it.get("price", 0) or 0)
        cat   = str(it.get("category", "Other")).strip() or "Other"
        exp   = Expense(
            title       = name,
            amount      = round(price, 2),
            category    = cat,
            description = "From receipt scan",
            date        = today,
            user_id     = user_id
        )
        db.session.add(exp)
        expenses.append(exp)

    db.session.commit()
    return jsonify({"added": len(expenses), "expenses": [e.to_dict() for e in expenses]}), 201


# ── Chat Route ────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
@jwt_required()
def chat():
    user_id = int(get_jwt_identity())
    message = (request.get_json() or {}).get("message", "").strip()
    if not message:
        return jsonify({"error": "Empty message"}), 400

    cutoff   = (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
    expenses = (
        Expense.query
        .filter(Expense.user_id == user_id, Expense.date >= cutoff)
        .order_by(Expense.date.desc())
        .limit(100)
        .all()
    )

    if expenses:
        lines = "\n".join(
            f"- {e.date} | {e.title} | {e.category} | ₹{e.amount}"
            for e in expenses
        )
        context = f"User's recent expenses (last 90 days, in INR):\n{lines}"
    else:
        context = "The user has no recorded expenses yet."

    system_prompt = (
        "You are a helpful personal expense assistant. "
        "Answer the user's question about their expenses concisely in plain text. "
        "Always use ₹ for amounts. If you list items, keep it brief.\n\n"
        + context
        + "\n\nUser question: "
        + message
    )

    try:
        reply = _groq(
            messages=[{"role": "user", "content": system_prompt}],
            model="llama-3.3-70b-versatile",
            max_tokens=512,
        )
    except Exception as e:
        return jsonify({"error": f"AI error: {str(e)}"}), 500

    return jsonify({"reply": reply})


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=10000, debug=True)