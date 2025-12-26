from flask import Flask, redirect, url_for

app = Flask(__name__)

@app.get("/")
def index():
    # Minimal-Startseite (Ingress Test)
    return """
    <html>
      <body>
        <h1>Tado Assistant (Ingress)</h1>
        <p>Web läuft über Gunicorn (kein Debug/Reloader).</p>
        <form method="post" action="/auth/start">
          <button type="submit">Auth Start (Dummy)</button>
        </form>
      </body>
    </html>
    """

@app.post("/auth/start")
def auth_start():
    # Nur Test: zurück zur Startseite
    return redirect(url_for("index"), code=302)
