"""
Pizza Poll -- a tiny Flask app for ranked pizza voting, with four
switchable variants:

  #1 fixed     a curated list of exactly 4 pizzas, nothing else allowed
  #2 open      no preset list at all -- every option is typed in by a
               voter, and it sticks around for everyone after that
  #3 filtered  same as "open", but a new submission has a 1-in-3
               chance of being randomly rejected. The rejection popup
               names the real mechanism (a dice roll) rather than
               inventing a false reason for it.
  #4 hawaiian  the same fixed list of 4 as "fixed", but ranked by a
               weighted formula instead of a plain vote count: every
               pizza defaults to weight x1 and baseline +0, except
               Hawaiian, which is set to weight x10 and baseline +1.
               The formula and every pizza's real weight/baseline/vote
               numbers are always shown, so the math is checkable by
               hand rather than hidden behind the ranking.

No accounts, no cookies, no per-visitor tracking: anyone who opens the
page can cast a vote. Votes are (1st choice, 2nd choice) pairs, stored
per-variant in a small SQLite database (poll.db).
"""

import argparse
import random
import sqlite3
from pathlib import Path

from flask import Flask, g, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.secret_key = "pizza-poll-dev-key"  # only signs flash messages, fine for local use

DB_PATH = Path(__file__).parent / "poll.db"

MAX_CUSTOM_NAME_LEN = 40

# Sentinel returned by resolve_choice() when a submission is randomly
# rejected by a variant's filter, distinct from None (an invalid pick).
FILTERED = object()

VARIANTS = {
    "fixed": {
        "label": "#1",
        "allow_custom": False,
        "note": None,
        "formula": None,
        "always_show_results": False,
        "filter_reject_chance": None,
    },
    "open": {
        "label": "#2",
        "allow_custom": True,
        "note": None,
        "formula": None,
        "always_show_results": False,
        "filter_reject_chance": None,
    },
    "filtered": {
        "label": "#3",
        "allow_custom": True,
        "note": None,
        "formula": None,
        "always_show_results": False,
        "filter_reject_chance": 1 / 3,
    },
    "hawaiian": {
        "label": "#4",
        "allow_custom": False,
        "note": None,
        "formula": {
            "weight_of": {"Hawaiian": 10},
            "default_weight": 1,
            "baseline_of": {"Hawaiian": 1},
            "default_baseline": 0,
        },
        "always_show_results": True,
        "filter_reject_chance": None,
    },
}
DEFAULT_VARIANT = "fixed"
FIXED_PIZZAS = ["Margherita", "Pepperoni", "Hawaiian", "Veggie Supreme"]


# --- Database helpers -------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS pizzas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant TEXT NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE,
            is_permanent INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(variant, name)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant TEXT NOT NULL,
            first_choice TEXT NOT NULL,
            second_choice TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS poll_state (
            variant TEXT PRIMARY KEY,
            is_closed INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    for variant in VARIANTS:
        db.execute(
            "INSERT OR IGNORE INTO poll_state (variant, is_closed) VALUES (?, 0)",
            (variant,),
        )

    for name in FIXED_PIZZAS:
        db.execute(
            "INSERT OR IGNORE INTO pizzas (variant, name, is_permanent) VALUES ('fixed', ?, 1)",
            (name,),
        )
    for name in FIXED_PIZZAS:
        db.execute(
            "INSERT OR IGNORE INTO pizzas (variant, name, is_permanent) VALUES ('hawaiian', ?, 1)",
            (name,),
        )

    db.commit()
    db.close()


init_db()


# --- Variant + pizza-list helpers ---------------------------------------

def clean_variant(raw):
    return raw if raw in VARIANTS else DEFAULT_VARIANT


def get_pizzas(variant):
    rows = get_db().execute(
        "SELECT name, is_permanent FROM pizzas WHERE variant = ? "
        "ORDER BY is_permanent DESC, name COLLATE NOCASE ASC",
        (variant,),
    ).fetchall()
    return [{"name": r["name"], "pinned": bool(r["is_permanent"])} for r in rows]


def add_pizza(variant, name):
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO pizzas (variant, name, is_permanent) VALUES (?, ?, 0)",
        (variant, name),
    )
    db.commit()


def resolve_choice(variant, raw_value, custom_value):
    """Turn a submitted select value (plus optional custom text) into a
    canonical pizza name for this variant, adding it to the list if it's
    new. Returns None if the submission isn't valid, or the FILTERED
    sentinel if a new submission was randomly rejected by this variant's
    filter."""
    pizzas = {p["name"].lower(): p["name"] for p in get_pizzas(variant)}

    if raw_value == "__other__":
        if not VARIANTS[variant]["allow_custom"]:
            return None
        custom = (custom_value or "").strip()[:MAX_CUSTOM_NAME_LEN]
        if not custom:
            return None
        existing = pizzas.get(custom.lower())
        if existing:
            return existing

        reject_chance = VARIANTS[variant]["filter_reject_chance"]
        if reject_chance and random.random() < reject_chance:
            return FILTERED

        add_pizza(variant, custom)
        return custom

    return pizzas.get(raw_value.lower()) if raw_value else None


# --- Poll logic ---------------------------------------------------------

def is_poll_closed(variant):
    row = get_db().execute(
        "SELECT is_closed FROM poll_state WHERE variant = ?", (variant,)
    ).fetchone()
    return bool(row["is_closed"]) if row else False


def get_results(variant):
    """Tally votes and compute each pizza's displayed score.

    Most variants score honestly: 2 points for a 1st-choice pick, 1
    point for a 2nd-choice pick. Variants with a "formula" configured
    instead score by Score = (Votes + Baseline) x Weight, using the
    weight/baseline set per pizza (falling back to the variant's
    default weight/baseline for any pizza not explicitly listed).
    Either way, the underlying vote counts are always real and always
    returned alongside the score.
    """
    pizzas = get_pizzas(variant)
    pinned_lookup = {p["name"]: p["pinned"] for p in pizzas}

    rows = get_db().execute(
        "SELECT first_choice, second_choice FROM votes WHERE variant = ?", (variant,)
    ).fetchall()

    tally = {p["name"]: {"first": 0, "second": 0} for p in pizzas}
    for row in rows:
        if row["first_choice"] in tally:
            tally[row["first_choice"]]["first"] += 1
        if row["second_choice"] in tally:
            tally[row["second_choice"]]["second"] += 1

    formula = VARIANTS[variant]["formula"]

    results = []
    for name, t in tally.items():
        votes = t["first"] + t["second"]
        if formula:
            weight = formula["weight_of"].get(name, formula["default_weight"])
            baseline = formula["baseline_of"].get(name, formula["default_baseline"])
            points = (votes + baseline) * weight
        else:
            weight = None
            baseline = None
            points = t["first"] * 2 + t["second"]
        results.append(
            {
                "name": name,
                "first": t["first"],
                "second": t["second"],
                "votes": votes,
                "weight": weight,
                "baseline": baseline,
                "points": points,
                "pinned": pinned_lookup.get(name, False),
            }
        )

    max_points = max((r["points"] for r in results), default=0)
    for r in results:
        r["pct"] = round((r["points"] / max_points) * 100, 1) if max_points else 0

    results.sort(key=lambda r: (-r["points"], -r["votes"], r["name"]))
    return results, len(rows)


# --- Routes ---------------------------------------------------------------

@app.route("/")
def index():
    variant = clean_variant(request.args.get("variant", DEFAULT_VARIANT))
    results, total_voters = get_results(variant)
    top_points = results[0]["points"] if results else 0
    winners = [r["name"] for r in results if top_points > 0 and r["points"] == top_points]
    cfg = VARIANTS[variant]
    return render_template(
        "index.html",
        variant=variant,
        variants=VARIANTS,
        allow_custom=cfg["allow_custom"],
        variant_note=cfg["note"],
        weighted=cfg["formula"] is not None,
        always_show_results=cfg["always_show_results"],
        pizzas=get_pizzas(variant),
        results=results,
        total_voters=total_voters,
        closed=is_poll_closed(variant),
        winners=winners,
    )


@app.route("/results-partial")
def results_partial():
    variant = clean_variant(request.args.get("variant", DEFAULT_VARIANT))
    results, total_voters = get_results(variant)
    cfg = VARIANTS[variant]
    return render_template(
        "_results.html",
        results=results,
        total_voters=total_voters,
        allow_custom=cfg["allow_custom"],
        weighted=cfg["formula"] is not None,
        always_show_results=cfg["always_show_results"],
    )


@app.route("/vote", methods=["POST"])
def vote():
    variant = clean_variant(request.form.get("variant", DEFAULT_VARIANT))

    if is_poll_closed(variant):
        flash("This poll is closed and no longer accepting votes.")
        return redirect(url_for("index", variant=variant))

    first_custom_raw = request.form.get("first_choice_custom", "")
    second_custom_raw = request.form.get("second_choice_custom", "")

    first_choice = resolve_choice(
        variant, request.form.get("first_choice", ""), first_custom_raw
    )
    second_choice = resolve_choice(
        variant, request.form.get("second_choice", ""), second_custom_raw
    )

    if first_choice is FILTERED or second_choice is FILTERED:
        rejected_name = (
            first_custom_raw.strip() if first_choice is FILTERED else second_custom_raw.strip()
        )
        flash(rejected_name, "filtered")
        return redirect(url_for("index", variant=variant))

    if first_choice is None or second_choice is None:
        flash("Please choose two pizzas (or type your own, if this variant allows it).")
        return redirect(url_for("index", variant=variant))

    if first_choice == second_choice:
        flash("Your 1st and 2nd choice must be different pizzas.")
        return redirect(url_for("index", variant=variant))

    db = get_db()
    db.execute(
        "INSERT INTO votes (variant, first_choice, second_choice) VALUES (?, ?, ?)",
        (variant, first_choice, second_choice),
    )
    db.commit()
    return redirect(url_for("index", variant=variant))


@app.route("/close", methods=["POST"])
def close_poll():
    variant = clean_variant(request.form.get("variant", DEFAULT_VARIANT))
    db = get_db()
    db.execute("UPDATE poll_state SET is_closed = 1 WHERE variant = ?", (variant,))
    db.commit()
    return redirect(url_for("index", variant=variant))


@app.route("/reset", methods=["POST"])
def reset_poll():
    variant = clean_variant(request.form.get("variant", DEFAULT_VARIANT))
    db = get_db()
    db.execute("DELETE FROM votes WHERE variant = ?", (variant,))
    db.execute("DELETE FROM pizzas WHERE variant = ? AND is_permanent = 0", (variant,))
    db.execute("UPDATE poll_state SET is_closed = 0 WHERE variant = ?", (variant,))
    db.commit()
    return redirect(url_for("index", variant=variant))


def run_with_ngrok(port):
    """Open a public ngrok tunnel to `port`, then run the Flask app until
    it's stopped, tearing the tunnel down afterward either way."""
    try:
        from pyngrok import ngrok
    except ImportError:
        raise SystemExit(
            "pyngrok isn't installed. Run: pip install -r requirements-ngrok.txt"
        )

    try:
        tunnel = ngrok.connect(port, "http")
    except Exception as exc:  # noqa: BLE001 -- surfacing a clear message matters more here
        raise SystemExit(
            f"Couldn't start the ngrok tunnel ({exc}).\n"
            "Make sure you've run 'ngrok config add-authtoken YOUR_TOKEN' at "
            "least once (a free token is available at https://ngrok.com)."
        )

    print(f" * Public URL (ngrok): {tunnel.public_url}")
    print(" * Share that link with friends -- anyone with it can vote, close,")
    print("   or reset the poll, so only send it to people you want voting.")
    print(" * Ctrl+C stops both the tunnel and the server.")

    try:
        app.run(host="127.0.0.1", port=port, debug=False)
    finally:
        ngrok.kill()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the pizza poll server.")
    parser.add_argument(
        "--port", type=int, default=5000, help="Port to run on (default: 5000)"
    )
    parser.add_argument(
        "--ngrok",
        action="store_true",
        help=(
            "Also open a public ngrok tunnel to this server, so people off "
            "your network can vote (requires pyngrok and a free ngrok account)"
        ),
    )
    args = parser.parse_args()

    if args.ngrok:
        run_with_ngrok(args.port)
    else:
        app.run(host="127.0.0.1", port=args.port, debug=False)
