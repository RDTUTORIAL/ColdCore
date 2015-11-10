from flask import Flask, render_template, session, redirect, url_for, request, g, flash
app = Flask(__name__)
app.secret_key = "nope"

from database import Team, TeamAccess, Challenge, ChallengeSolve, ChallengeFailure, ScoreAdjustment, db
from datetime import datetime
from peewee import fn
import config
import utils
import redis
import requests

import logging
logging.basicConfig(level=logging.DEBUG)

@app.before_request
def make_info_available():
    if "team_id" in session:
        g.team = Team.get(Team.id == session["team_id"])

@app.context_processor
def scoreboard_variables():
    var = dict(config=config)
    if "team_id" in session:
        var["logged_in"] = True
        var["team"] = g.team
    else:
        var["logged_in"] = False

    return var

# Publically accessible things

@app.route('/')
def root():
    return redirect(url_for('scoreboard'))

@app.route('/scoreboard/')
def scoreboard():
    data = utils.get_complex("scoreboard")
    graphdata = utils.get_complex("graph")
    if not data or not graphdata:
        data = utils.calculate_scores()
        graphdata = utils.calculate_graph(data)
        utils.set_complex("scoreboard", data, 1)
        utils.set_complex("graph", graphdata, 1)

    afdata = ChallengeSolve.select(ChallengeSolve, Challenge, Team).join(Challenge).join(Team, on=Team.id == ChallengeSolve.team).order_by(ChallengeSolve.time.desc()).limit(8)
    return render_template("scoreboard.html", data=data, graphdata=graphdata, afdata=afdata)

@app.route('/login/', methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    elif request.method == "POST":
        team_key = request.form["team_key"]

        try:
            team = Team.get(Team.key == team_key)
            TeamAccess.create(team=team, ip=utils.get_ip(), time=datetime.now())
            session["team_id"] = team.id
            flash("Login success.")
            return redirect(url_for('dashboard'))
        except Team.DoesNotExist:
            flash("Couldn't find your team. Check your team key.", "error")
            return render_template("login.html")

@app.route('/register/', methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")
    elif request.method == "POST":
        if "g-recaptcha-response" not in request.form:
            flash("Please complete the CAPTCHA.")
            return render_template("register.html")

        captcha_response = request.form["g-recaptcha-response"]
        verify_data = dict(secret=config.secret.recaptcha_secret, response=captcha_response, remoteip=utils.get_ip())
        result = requests.post("https://www.google.com/recaptcha/api/siteverify", verify_data).json()["success"]
        if not result:
            flash("Invalid CAPTCHA response.")
            return render_template("register.html")

        team_name = request.form["team_name"]
        team_email = request.form["team_email"]
        team_elig = "team_eligibility" in request.form
        affiliation = request.form["affiliation"]
        team_key = utils.generate_team_key()
        confirmation_key = utils.generate_confirmation_key()
        team = Team.create(name=team_name, email=team_email, eligible=team_elig, affiliation=affiliation, key=team_key,
                           email_confirmation_key=confirmation_key)
        TeamAccess.create(team=team, ip=utils.get_ip(), time=datetime.now())

        utils.send_confirmation_email(team_email, confirmation_key, team_key)

        session["team_id"] = team.id
        flash("Team created.")
        return redirect(url_for('dashboard'))

@app.route('/logout/')
def logout():
    session.pop("team_id")
    flash("You've successfully logged out.")
    return redirect(url_for('root'))

# Debugging things

@app.route('/assign-random/')
def assign_random():
    if not app.debug:
        return "Nope."
    session["team_id"] = Team.select().order_by(fn.Random()).get().id
    return "OK"

# Things that require a team

@app.route('/confirm_email/', methods=["POST"])
@utils.login_required
def confirm_email():
    if request.form["confirmation_key"] == g.team.email_confirmation_key:
        flash("Email confirmed!")
        g.team.email_confirmed = True
        g.team.save()
    else:
        flash("Incorrect confirmation key.")
    return redirect(url_for('dashboard'))

@app.route('/team/', methods=["GET", "POST"])
@utils.login_required
def dashboard():
    if request.method == "GET":
        team_solves = ChallengeSolve.select(ChallengeSolve, Challenge).join(Challenge).where(ChallengeSolve.team == g.team)
        team_adjustments = ScoreAdjustment.select().where(ScoreAdjustment.team == g.team)
        team_score = sum([i.challenge.points for i in team_solves] + [i.value for i in team_adjustments])
        first_login = False
        if g.team.first_login:
            first_login = True
            g.team.first_login = False
            g.team.save()
        return render_template("dashboard.html", team_solves=team_solves, team_adjustments=team_adjustments, team_score=team_score, first_login=first_login)
    elif request.method == "POST":
        if g.redis.get("ul{}".format(session["team_id"])):
            flash("You're changing your information too fast!")
            return redirect(url_for('dashboard'))

        g.redis.set("ul{}".format(session["team_id"]), str(datetime.now()), 120)
        team_name = request.form["team_name"]
        team_email = request.form["team_email"]
        affiliation = request.form["affiliation"]
        team_elig = "team_eligibility" in request.form

        email_changed = (team_email != g.team.email)

        g.team.name = team_name
        g.team.email = team_email
        g.team.affiliation = affiliation
        g.team.eligible = team_elig
        if email_changed:
            g.team.email_confirmation_key = utils.generate_confirmation_key()
            g.team.email_confirmed = False
            utils.send_confirmation_email(team_email, g.team.email_confirmation_key, g.team.key)
            flash("Changes saved. Please check your email for a new confirmation key.")
        else:
            flash("Changes saved.")
        g.team.save()


        return redirect(url_for('dashboard'))

@app.route('/challenges/')
@utils.competition_running_required
@utils.confirmed_email_required
def challenges():
    chals = Challenge.select().order_by(Challenge.points)
    solved = Challenge.select().join(ChallengeSolve).where(ChallengeSolve.team == g.team)
    return render_template("challenges.html", challenges=chals, solved=solved)

@app.route('/submit/<int:challenge>/', methods=["POST"])
@utils.competition_running_required
@utils.confirmed_email_required
def submit(challenge):
    chal = Challenge.get(Challenge.id == challenge)
    flag = request.form["flag"]

    if g.redis.get("rl{}".format(g.team.id)):
        return "You're submitting flags too fast!"

    if g.team.solved(chal):
        flash("You've already solved that problem!")
    elif chal.flag != flag:
        flash("Incorrect flag.")
        g.redis.set("rl{}".format(g.team.id), str(datetime.now()), config.flag_rl)
        ChallengeFailure.create(team=g.team, challenge=chal, attempt=flag, time=datetime.now())
    else:
        flash("Correct!")
        g.redis.delete("scoreboard")
        ChallengeSolve.create(team=g.team, challenge=chal, time=datetime.now())
    return redirect(url_for('challenges'))

# Manage Peewee database sessions and Redis

@app.before_request
def before_request():
    db.connect()
    g.redis = redis.StrictRedis()

@app.teardown_request
def teardown_request(exc):
    db.close()
    g.redis.connection_pool.disconnect()

# CSRF things

@app.before_request
def csrf_protect():
    if app.debug:
        return
    if request.method == "POST":
        token = session.pop('_csrf_token', None)
        if not token or token != request.form["_csrf_token"]:
            return "Invalid CSRF token!"

def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = utils.generate_random_string(64)
    return session['_csrf_token']

app.jinja_env.globals['csrf_token'] = generate_csrf_token

if __name__ == '__main__':
    app.run(debug=True, port=8001)