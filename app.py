import os
import sqlite3
import smtplib
from dotenv import load_dotenv

load_dotenv()
from email.message import EmailMessage
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, abort
)

app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-this-key")

DATABASE = os.path.join(app.root_path, "reviews.db")


# ---------- БАЗА ОТЗЫВОВ ----------

def get_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
        """)


init_db()


# ---------- ГЛАВНАЯ СТРАНИЦА ----------

@app.route("/", methods=["GET", "POST"])
def index():
    sent = False
    error = None

    # Обработка старой формы заявки, если она отправляется на /
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        consent = request.form.get("consent")

        if not name or not phone or consent != "yes":
            error = "Заполните имя и телефон и подтвердите согласие."
        else:
            print("\n--- Новая заявка ---")
            print("Время:", datetime.now().isoformat(timespec="seconds"))
            print("Имя:", name)
            print("Телефон:", phone)
            print("Email:", request.form.get("email", "").strip())
            print("Комментарий:", request.form.get("message", "").strip())
            print("--------------------\n")
            sent = True

    with get_db() as db:
        reviews = db.execute("""
            SELECT name, rating, text, created_at
            FROM reviews
            WHERE status = 'approved'
            ORDER BY id DESC
        """).fetchall()

    return render_template(
        "index.html",
        sent=sent,
        error=error,
        year=datetime.now().year,
        reviews=reviews
    )


# ---------- ОТПРАВКА ОТЗЫВА ----------

@app.route("/reviews", methods=["POST"])
def submit_review():
    name = request.form.get("review_name", "").strip()
    review_text = request.form.get("review_text", "").strip()
    rating_raw = request.form.get("rating", "")
    consent = request.form.get("review_consent")
    email = request.form.get("email", "").strip()
    try:
        rating = int(rating_raw)
    except (TypeError, ValueError):
        rating = 0

    if not name or not review_text:
        return "Заполните имя и текст отзыва.", 400

    if len(name) > 100 or len(review_text) > 2000:
        return "Имя или отзыв слишком длинные.", 400

    if rating not in range(1, 6):
        return "Выберите оценку от 1 до 5.", 400

    if consent != "yes":
        return "Необходимо согласие на обработку персональных данных.", 400

    with get_db() as db:
        db.execute("""
            INSERT INTO reviews
                (name, rating, text, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
        """, (
            name,
            rating,
            review_text,
            datetime.now().isoformat(timespec="seconds")
        ))

    flash("Спасибо! Отзыв отправлен на проверку.", "success")
    return redirect(url_for("index") + "#reviews")


# ---------- ВХОД АДМИНИСТРАТОРА ----------

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST" and not session.get("is_admin"):
        password = os.environ.get("ADMIN_PASSWORD")

        if not password:
            return "Не настроен пароль администратора.", 503

        if request.form.get("password", "") == password:
            session.clear()
            session["is_admin"] = True
            return redirect(url_for("admin"))

        flash("Неверный пароль.", "error")

    if not session.get("is_admin"):
        return render_template(
            "admin.html",
            logged_in=False,
            reviews=[]
        )

    with get_db() as db:
        reviews = db.execute("""
            SELECT id, name, rating, text, status, created_at
            FROM reviews
            ORDER BY
                CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                id DESC
        """).fetchall()

    return render_template(
        "admin.html",
        logged_in=True,
        reviews=reviews
    )


# ---------- ОДОБРЕНИЕ И СКРЫТИЕ ----------

@app.route("/admin/review/<int:review_id>/<action>", methods=["POST"])
@admin_required
def moderate_review(review_id, action):
    if action == "approve":
        status = "approved"
    elif action == "hide":
        status = "pending"
    else:
        abort(404)

    with get_db() as db:
        result = db.execute(
            "UPDATE reviews SET status = ? WHERE id = ?",
            (status, review_id)
        )

    if result.rowcount == 0:
        abort(404)

    flash("Статус отзыва изменён.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for("admin"))


# ---------- ОТПРАВКА ЗАЯВКИ НА ПОЧТУ ----------

@app.route("/submit", methods=["POST"])
def submit():
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    message = request.form.get("message", "").strip()
    consent = request.form.get("consent")

    if not name or not phone or not email:
        return "Заполните имя, телефон и E-mail.", 400

    if consent != "yes":
        return "Необходимо согласие на обработку персональных данных.", 400

    answers = []
    for i in range(1, 16):
        selected = request.form.getlist(f"q{i}")
        answers.append(
            f"Вопрос {i}: {', '.join(selected) if selected else 'Не отвечено'}"
        )

    questionnaire_text = "\n".join(answers)

    email_user = os.environ.get("MAIL_USER")
    email_password = os.environ.get("MAIL_PASSWORD")
    email_to = os.environ.get("MAIL_TO")

    if not email_user or not email_password or not email_to:
        app.logger.error("Не настроены переменные почты")
        return "Отправка заявок временно недоступна.", 500

    letter = EmailMessage()
    letter["Subject"] = "Новая заявка с сайта"
    letter["From"] = email_user
    letter["To"] = email_to

    letter.set_content(
        f"Новая заявка с сайта\n\n"
        f"Имя: {name}\n"
        f"Телефон: {phone}\n"
        f"E-mail: {email}\n"
        f"Комментарий: {message}\n\n"
        f"Ответы на анкету:\n{questionnaire_text}"
    )

    try:
        with smtplib.SMTP_SSL("smtp.yandex.ru", 465) as server:
            server.login(email_user, email_password)
            server.send_message(letter)
    except Exception:
        app.logger.exception("Не удалось отправить заявку")
        return "Не удалось отправить заявку. Попробуйте позже.", 500

    return """
        <h2>Спасибо! Заявка отправлена.</h2>
        <a href="/">Вернуться на сайт</a>
    """


# ---------- ПОЛИТИКА ----------

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/consent")
def consent():
    return render_template("consent.html")

if __name__ == "__main__":
    app.run(debug=True)
