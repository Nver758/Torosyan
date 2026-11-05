"""APAGA Training Evaluation server.

Run locally:
  pip install -r requirements.txt
  ADMIN_PASSWORD='change-this' FLASK_SECRET_KEY='replace-me' python app.py

Production: set DATABASE_URL to a durable PostgreSQL database, ADMIN_PASSWORD,
FLASK_SECRET_KEY; optionally OPENAI_API_KEY and SMTP_* for extra features.
"""
import os
import re
import io
import csv
import json
import uuid
import time
import ssl
import smtplib
import sqlite3
import secrets
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, Response, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import safe_join

BASE = Path(__file__).resolve().parent
PUBLIC_DIR = BASE if (BASE / 'index.html').exists() else BASE / 'public'
app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('COOKIE_SECURE', '0') == '1'
app.config['MAX_CONTENT_LENGTH'] = 80 * 1024
app.json.ensure_ascii = False
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
SQLITE_PATH = os.getenv('SQLITE_PATH', str(BASE / 'apaga_responses.sqlite3'))
USING_POSTGRES = DATABASE_URL.startswith('postgres://') or DATABASE_URL.startswith('postgresql://')
TRAINERS = ['Ռոզա Սոլոմոնյան', 'Նարեկ Մարության', 'Նարինե Կարապետյան']
CRITERIA = [
    'Թեմայի համապատասխանությունն իմ հետաքրքրություններին և մասնագիտական կարիքներին',
    'Նյութի բացատրության հստակությունն ու հասկանալի մատուցումը',
    'Դասընթացավարի մասնագիտական փորձի փոխանցումը',
    'Գործնական օրինակներն ու իրական իրավիճակների քննարկումը',
    'Վարժություններն ու գործնական առաջադրանքները',
    'Քննարկումները և ուսանողների ակտիվ ներգրավումը',
    'Ուսանողների հարցերին տրված պարզաբանումները',
    'Դասընթացավարի վերաբերմունքն ուսանողների նկատմամբ',
    'Գործնական խնդրի լուծման համար կիրառելի կոնկրետ գործիք, մեթոդ կամ քայլերի հաջորդականության ներկայացումը',
    'Ինքնուրույն իրավիճակ վերլուծելու, լուծում առաջարկելու կամ ընտրությունը հիմնավորելու հնարավորությունը',
    'Ստացած գիտելիքի կամ մոտեցման կիրառելիությունը',
]
QUESTIONS = [
    'Ի՞նչ դասընթաց կցանկանաք, որ լինի «Կրթությունից աշխատանք՝ մեկ քայլ» ծրագրի շրջանակներում։',
    'Ո՞ւմ կցանկանաք տեսնել որպես դասընթացավար «Կրթությունից աշխատանք՝ մեկ քայլ» ծրագրի շրջանակներում։',
    'Ի՞նչ փոփոխություն կառաջարկեք ծրագրի դասընթացներում՝ դրանք ավելի հետաքրքիր կամ օգտակար դարձնելու համար։',
    'Ինչո՞ւ եք անհրաժեշտ համարում այդ փոփոխությունները։ Ի՞նչ արդյունք եք ակնկալում դրանց իրականացումից։',
    '«Կրթությունից աշխատանք՝ մեկ քայլ» ծրագրի ո՞ր ուղղություններում եք փոփոխությունների անհրաժեշտություն տեսնում։',
    '«Կրթությունից աշխատանք՝ մեկ քայլ» ծրագրի վերաբերյալ ի՞նչ դիտարկում կամ առաջարկ ունեք։',
    'Ի՞նչ հնարավորություն, թեմա կամ գործունեություն կցանկանայիք ավելացնել ծրագրում։',
]
_rate = defaultdict(list)


DEFAULT_SCHEMA = dict(
    title='Ձեր կարծիքը փոխում է վաղվա դասընթացը։',
    description='Գնահատեք այն դասընթացները, որոնց մասնակցել եք, և առաջարկեք՝ ինչն արժե պահպանել կամ բարելավել։ Ձեր գնահատականները և առաջարկները կօգտագործվեն ծրագրի ներքին բարելավման համար։',
    program='«ԿՐԹՈՒԹՅՈՒՆԻՑ ԱՇԽԱՏԱՆՔ՝ ՄԵԿ ՔԱՅԼ»',
    trainers=TRAINERS[:], criteria=CRITERIA[:], questions=QUESTIONS[:], formOpen=True
)


def list_versions():
    rows = execute('SELECT version, created_at, config_json FROM survey_versions ORDER BY version DESC', fetch=True)
    return [dict(version=int(r[0]), createdAt=r[1], **json.loads(r[2])) for r in rows]


def latest_schema():
    return list_versions()[0]


def schema_for(version):
    rows = execute('SELECT version, created_at, config_json FROM survey_versions WHERE version=?', (version,), fetch=True)
    if not rows:
        return None
    return dict(version=int(rows[0][0]), createdAt=rows[0][1], **json.loads(rows[0][2]))


def validate_schema(raw):
    if not isinstance(raw, dict):
        return None, 'Կարգավորումների սխալ ձևաչափ'
    obj = {}
    for field, maximum in [('title', 180), ('description', 900), ('program', 180)]:
        val = raw.get(field)
        if not isinstance(val, str) or not val.strip() or len(val.strip()) > maximum:
            return None, f'Սխալ դաշտ՝ {field}'
        obj[field] = val.strip()
    for field, minimum, maximum, max_chars in [('trainers', 1, 15, 120), ('criteria', 1, 20, 500), ('questions', 1, 20, 600)]:
        items = raw.get(field)
        if not isinstance(items, list) or not minimum <= len(items) <= maximum:
            return None, f'{field}: թույլատրվում է {minimum}–{maximum} գրառում'
        if any(not isinstance(x, str) or not x.strip() or len(x.strip()) > max_chars for x in items):
            return None, f'{field}: բոլոր տողերը պետք է լրացված լինեն'
        obj[field] = [x.strip() for x in items]
        if len(set(obj[field])) != len(obj[field]):
            return None, f'{field}: կրկնվող տողեր կան'
    if type(raw.get('formOpen')) is not bool:
        return None, 'Սխալ դաշտ՝ formOpen'
    obj['formOpen'] = raw['formOpen']
    return obj, None


def db_open():
    if USING_POSTGRES:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    conn = sqlite3.connect(SQLITE_PATH)
    return conn


def execute(sql, args=(), fetch=False):
    conn = db_open()
    try:
        cur = conn.cursor()
        if USING_POSTGRES:
            sql = sql.replace('?', '%s')
        cur.execute(sql, args)
        rows = cur.fetchall() if fetch else []
        conn.commit()
        return rows
    finally:
        conn.close()


def init_db():
    execute('''CREATE TABLE IF NOT EXISTS submissions (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT,
        ratings_json TEXT NOT NULL,
        feedback_json TEXT NOT NULL
    )''')
    # Preserve existing V3 submissions and assign their responses to the original questionnaire (version 1).
    conn = db_open()
    try:
        cur = conn.cursor()
        if USING_POSTGRES:
            cur.execute('ALTER TABLE submissions ADD COLUMN IF NOT EXISTS survey_version INTEGER NOT NULL DEFAULT 1')
        else:
            cur.execute('PRAGMA table_info(submissions)')
            if 'survey_version' not in [row[1] for row in cur.fetchall()]:
                cur.execute('ALTER TABLE submissions ADD COLUMN survey_version INTEGER NOT NULL DEFAULT 1')
        conn.commit()
    finally:
        conn.close()
    execute('''CREATE TABLE IF NOT EXISTS survey_versions (
        version INTEGER PRIMARY KEY, created_at TEXT NOT NULL, config_json TEXT NOT NULL
    )''')
    if not execute('SELECT version FROM survey_versions LIMIT 1', fetch=True):
        execute('INSERT INTO survey_versions (version,created_at,config_json) VALUES (?,?,?)',
                (1, now(), json.dumps(DEFAULT_SCHEMA, ensure_ascii=False)))
    execute('''CREATE TABLE IF NOT EXISTS improvement_actions (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        title TEXT NOT NULL,
        owner TEXT NOT NULL,
        due_date TEXT NOT NULL,
        priority TEXT NOT NULL,
        status TEXT NOT NULL,
        note TEXT NOT NULL
    )''')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def is_admin():
    return session.get('apaga_admin') is True and bool(ADMIN_PASSWORD)


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not is_admin():
            return jsonify(error='Ադմինիստրատորի մուտքը պարտադիր է'), 401
        return fn(*args, **kwargs)
    return wrapped


def rate_limited(bucket, maximum, period=3600):
    """Lightweight per-process protection; use an upstream gateway for high traffic."""
    ip = request.remote_addr or 'unknown'
    key = bucket + ':' + ip
    current = time.monotonic()
    _rate[key] = [x for x in _rate[key] if current - x < period]
    if len(_rate[key]) >= maximum:
        return True
    _rate[key].append(current)
    return False


def validate_payload(payload):
    if not isinstance(payload, dict):
        return 'Սխալ տվյալներ'
    for name in ('firstName', 'lastName'):
        if not isinstance(payload.get(name), str) or not 1 <= len(payload[name].strip()) <= 120:
            return 'Անունն ու ազգանունը պարտադիր են'
    email = payload.get('email', '')
    if not isinstance(email, str) or len(email) > 180 or (email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)):
        return 'Էլեկտրոնային հասցեն սխալ է'
    active = latest_schema()
    if payload.get('schemaVersion') != active['version']:
        return 'Հարցաշարը փոխվել է։ Խնդրում ենք թարմացնել էջը և նորից լրացնել։'
    if not active['formOpen']:
        return 'Հարցաշարը ժամանակավորապես փակ է'
    ratings = payload.get('ratings')
    if not isinstance(ratings, list) or len(ratings) != len(active['trainers']):
        return 'Դասընթացավարների քանակը սխալ է'
    for t in ratings:
        if not isinstance(t, dict) or not isinstance(t.get('absent'), bool):
            return 'Սխալ գնահատման կառուցվածք'
        comment = t.get('comment', '')
        if not isinstance(comment, str) or len(comment) > 1500:
            return 'Դասընթացավարի մեկնաբանությունը չափազանց երկար է'
        scores = t.get('scores')
        if not isinstance(scores, list) or len(scores) != len(active['criteria']):
            return 'Գնահատման չափանիշների քանակը սխալ է'
        if t['absent']:
            if any(v is not None for v in scores):
                return 'Բացակայության դեպքում գնահատականները պետք է դատարկ լինեն'
        elif not all(type(v) is int and 1 <= v <= 5 for v in scores):
            return 'Մասնակցած դասընթացի բոլոր գնահատականները պարտադիր են'
    feedback = payload.get('feedback')
    if not isinstance(feedback, list) or len(feedback) != len(active['questions']) or any(not isinstance(x, str) or len(x) > 2000 for x in feedback):
        return 'Առաջարկների ձևաչափը սխալ է'
    if payload.get('consent') is not True:
        return 'Տվյալների մշակման համաձայնությունը պարտադիր է'
    return None


def all_submissions():
    rows = execute('SELECT id, created_at, first_name, last_name, email, ratings_json, feedback_json, survey_version FROM submissions ORDER BY created_at DESC', fetch=True)
    return [dict(id=r[0], createdAt=r[1], firstName=r[2], lastName=r[3], email=r[4], ratings=json.loads(r[5]), feedback=json.loads(r[6]), surveyVersion=int(r[7])) for r in rows]


def analyze(records, schema=None):
    schema = schema or latest_schema()
    tsummary = []
    all_scores = []
    for i, name in enumerate(schema['trainers']):
        present = [r['ratings'][i] for r in records if not r['ratings'][i]['absent']]
        flat = [v for t in present for v in t['scores']]
        all_scores.extend(flat)
        tsummary.append(dict(name=name, count=len(present), average=round(sum(flat)/len(flat), 3) if flat else None,
                             criteria=[round(sum(t['scores'][j] for t in present)/len(present), 3) if present else None for j in range(len(schema['criteria']))]))
    comments = [a.strip() for r in records for a in r.get('feedback', []) if a.strip()]
    return dict(responses=len(records), overall_average=round(sum(all_scores)/len(all_scores), 3) if all_scores else None,
                trainers=tsummary, suggestions=len(comments), comments=comments)


@app.after_request
def security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    if request.path.startswith('/api/admin'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.get('/static/<path:filename>')
def static_assets(filename):
    if filename not in ('app.js', 'styles.css'):
        return jsonify(error='Ֆայլը հասանելի չէ'), 404
    return send_from_directory(PUBLIC_DIR, filename)


@app.get('/')
@app.get('/cv')
@app.get('/evaluation')
@app.get('/admin')
def index():
    return send_from_directory(PUBLIC_DIR, 'index.html')


@app.get('/api/config')
def config():
    return jsonify(mode='server', aiEnabled=bool(os.getenv('OPENAI_API_KEY')),
                   emailEnabled=bool(os.getenv('SMTP_HOST') and os.getenv('SMTP_FROM')),
                   schema=latest_schema())


@app.post('/api/submissions')
def submit():
    if rate_limited('submit', 60):
        return jsonify(error='Չափազանց շատ հարցումներ։ Փորձեք ավելի ուշ։'), 429
    payload = request.get_json(silent=True)
    problem = validate_payload(payload)
    if problem:
        return jsonify(error=problem), 409 if isinstance(payload,dict) and payload.get('schemaVersion') != latest_schema()['version'] else 400
    uid = str(uuid.uuid4())
    schema = latest_schema()
    execute('INSERT INTO submissions (id, created_at, first_name, last_name, email, ratings_json, feedback_json, survey_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (uid, now(), payload['firstName'].strip(), payload['lastName'].strip(), payload.get('email', '').strip(),
             json.dumps(payload['ratings'], ensure_ascii=False), json.dumps(payload['feedback'], ensure_ascii=False), schema['version']))
    return jsonify(ok=True, id=uid), 201


@app.post('/api/admin/login')
def admin_login():
    if rate_limited('login', 12, 900):
        return jsonify(error='Շատ փորձեր։ Կրկին փորձեք 15 րոպե անց։'), 429
    if not ADMIN_PASSWORD:
        return jsonify(error='Ադմինիստրատորի մուտքը դեռ կարգավորված չէ։ Սահմանեք ADMIN_PASSWORD։'), 503
    incoming = (request.get_json(silent=True) or {}).get('password', '')
    if isinstance(incoming, str) and secrets.compare_digest(incoming, ADMIN_PASSWORD):
        session.clear()
        session['apaga_admin'] = True
        return jsonify(ok=True)
    return jsonify(error='Գաղտնաբառը սխալ է'), 401


@app.post('/api/admin/logout')
def admin_logout():
    session.clear()
    return jsonify(ok=True)


@app.get('/api/admin/status')
def admin_status():
    return jsonify(loggedIn=is_admin())


@app.get('/api/admin/config/versions')
@admin_required
def admin_config_versions():
    return jsonify(versions=list_versions())


@app.post('/api/admin/config')
@admin_required
def admin_update_config():
    # Updating the questionnaire creates a new immutable version. Never rewrite past answers.
    if rate_limited('config-edit', 40, 3600):
        return jsonify(error='Չափազանց շատ փոփոխություններ։ Փորձեք ավելի ուշ։'), 429
    raw = request.get_json(silent=True)
    cleaned, problem = validate_schema(raw)
    if problem:
        return jsonify(error=problem), 400
    current = latest_schema()
    if raw.get('baseVersion') != current['version']:
        return jsonify(error='Կարգավորումներն արդեն փոխվել են մեկ այլ պատուհանում։ Թարմացրեք էջը։'), 409
    if all(current.get(k) == cleaned[k] for k in cleaned):
        return jsonify(ok=True, schema=current)
    version = current['version'] + 1
    execute('INSERT INTO survey_versions (version,created_at,config_json) VALUES (?,?,?)',
            (version, now(), json.dumps(cleaned, ensure_ascii=False)))
    return jsonify(ok=True, schema=latest_schema()), 201


@app.get('/api/admin/submissions')
@admin_required
def admin_submissions():
    return jsonify(submissions=all_submissions())


def fetch_submission_by_id(uid):
    rows = execute('SELECT id, created_at, first_name, last_name, email, ratings_json, feedback_json, survey_version FROM submissions WHERE id=?', (uid,), fetch=True)
    if not rows:
        return None
    r = rows[0]
    return dict(id=r[0], createdAt=r[1], firstName=r[2], lastName=r[3], email=r[4], ratings=json.loads(r[5]), feedback=json.loads(r[6]), surveyVersion=int(r[7]))


def export_rows_csv(records, trainer_indices=None):
    """Semicolon CSV, version-safe: column labels are stored with the survey version."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    w.writerow(['Պատասխանի ID','Ամսաթիվ','Անուն','Ազգանուն','Տարբերակ','Դասընթացավար',
                'Մասնակցել է','Միջին','Չափանիշ','Միավոր','Դասընթացավարի մեկնաբանություն',
                'Հարցաշարի բաց պատասխանները (JSON)'])
    schemas = {v['version']:v for v in list_versions()}
    for r in records:
        schema = schemas.get(r['surveyVersion']) or schema_for(1)
        indexes = trainer_indices if trainer_indices is not None else range(len(schema['trainers']))
        for i in indexes:
            if i >= len(schema['trainers']) or i >= len(r['ratings']): continue
            t=r['ratings'][i]
            scores=[v for v in t['scores'] if type(v) is int]
            average=round(sum(scores)/len(scores),3) if scores and not t['absent'] else ''
            feedback = json.dumps([{'question':q,'answer':r['feedback'][j] if j<len(r['feedback']) else ''} for j,q in enumerate(schema['questions'])],ensure_ascii=False)
            for j,criterion in enumerate(schema['criteria']):
                value = t['scores'][j] if j < len(t['scores']) else ''
                row=[r['id'],r['createdAt'],r['firstName'],r['lastName'],r['surveyVersion'],schema['trainers'][i],
                     'Ոչ' if t['absent'] else 'Այո',average,criterion,value if value is not None else '',t.get('comment',''),feedback]
                w.writerow([safe_excel(v) for v in row])
    return '\ufeff' + buf.getvalue()


@app.get('/api/admin/submissions/<submission_id>')
@admin_required
def admin_one_submission(submission_id):
    record = fetch_submission_by_id(submission_id)
    return jsonify(submission=record) if record else (jsonify(error='Պատասխանը չի գտնվել'), 404)


@app.get('/api/admin/submissions/<submission_id>.csv')
@admin_required
def admin_one_csv(submission_id):
    record = fetch_submission_by_id(submission_id)
    if not record:
        return jsonify(error='Պատասխանը չի գտնվել'), 404
    return Response(export_rows_csv([record]), content_type='text/csv; charset=utf-8',
        headers={'Content-Disposition':f'attachment; filename="apaga-submission-{record["id"]}.csv"'})


@app.get('/api/admin/trainers/<int:trainer_index>/submissions')
@admin_required
def admin_trainer_submissions(trainer_index):
    version = request.args.get('version', type=int) or latest_schema()['version']
    schema = schema_for(version)
    if not schema or trainer_index < 0 or trainer_index >= len(schema['trainers']):
        return jsonify(error='Դասընթացավարը չի գտնվել'), 404
    records = []
    for r in all_submissions():
        if r['surveyVersion'] != version: continue
        t = r['ratings'][trainer_index]
        scores = [v for v in t['scores'] if type(v) is int]
        records.append(dict(submissionId=r['id'], createdAt=r['createdAt'], firstName=r['firstName'],
                            lastName=r['lastName'], trainer=schema['trainers'][trainer_index], absent=t['absent'],
                            scores=t['scores'], average=round(sum(scores)/len(scores), 3) if scores and not t['absent'] else None,
                            comment=t.get('comment','')))
    return jsonify(trainer=schema['trainers'][trainer_index], entries=records)


@app.get('/api/admin/trainers/<int:trainer_index>/export.csv')
@admin_required
def admin_trainer_csv(trainer_index):
    version = request.args.get('version', type=int) or latest_schema()['version']
    schema = schema_for(version)
    if not schema or trainer_index < 0 or trainer_index >= len(schema['trainers']):
        return jsonify(error='Դասընթացավարը չի գտնվել'), 404
    return Response(export_rows_csv([r for r in all_submissions() if r['surveyVersion'] == version], [trainer_index]), content_type='text/csv; charset=utf-8',
        headers={'Content-Disposition':f'attachment; filename="apaga-trainer-{trainer_index + 1}.csv"'})


@app.get('/api/admin/actions')
@admin_required
def get_actions():
    rows = execute('SELECT id, created_at, title, owner, due_date, priority, status, note FROM improvement_actions ORDER BY created_at DESC', fetch=True)
    return jsonify(actions=[dict(id=r[0], createdAt=r[1], title=r[2], owner=r[3], dueDate=r[4], priority=r[5], status=r[6], note=r[7]) for r in rows])


def clean_action(payload, patch=False):
    if not isinstance(payload, dict):
        return None, 'Տվյալների ձևաչափը սխալ է'
    fields = {'title':'title','owner':'owner','dueDate':'due_date','priority':'priority','status':'status','note':'note'}
    if patch and not any(k in payload for k in fields):
        return None, 'Փոփոխման դաշտեր նշված չեն'
    result = {}
    for src, dst in fields.items():
        if src not in payload:
            if patch: continue
            value = {'title':'','owner':'','dueDate':'','priority':'medium','status':'planned','note':''}[src]
        else:
            value = payload[src]
        if not isinstance(value,str) or len(value.strip()) > (500 if src=='note' else 180):
            return None, f'Սխալ դաշտ՝ {src}'
        value = value.strip()
        if src=='title' and not value:
            return None, 'Գործողության անվանումը պարտադիր է'
        if src=='dueDate' and value and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return None, 'Սխալ ժամկետ'
        if src=='priority' and value not in ('low','medium','high'):
            return None, 'Սխալ առաջնահերթություն'
        if src=='status' and value not in ('planned','progress','done'):
            return None, 'Սխալ կարգավիճակ'
        result[dst] = value
    return result, None


@app.post('/api/admin/actions')
@admin_required
def create_action():
    vals, err = clean_action(request.get_json(silent=True))
    if err: return jsonify(error=err), 400
    uid = str(uuid.uuid4())
    execute('INSERT INTO improvement_actions (id,created_at,title,owner,due_date,priority,status,note) VALUES (?,?,?,?,?,?,?,?)',
            (uid, now(), vals['title'], vals['owner'], vals['due_date'], vals['priority'], vals['status'], vals['note']))
    return jsonify(ok=True, id=uid), 201


@app.patch('/api/admin/actions/<action_id>')
@admin_required
def update_action(action_id):
    if not execute('SELECT id FROM improvement_actions WHERE id=?', (action_id,), fetch=True):
        return jsonify(error='Գործողությունը չի գտնվել'), 404
    vals, err = clean_action(request.get_json(silent=True), patch=True)
    if err: return jsonify(error=err), 400
    execute('UPDATE improvement_actions SET ' + ', '.join(k+'=?' for k in vals) + ' WHERE id=?', (*vals.values(), action_id))
    return jsonify(ok=True)


@app.delete('/api/admin/actions/<action_id>')
@admin_required
def delete_action(action_id):
    if not execute('SELECT id FROM improvement_actions WHERE id=?', (action_id,), fetch=True):
        return jsonify(error='Գործողությունը չի գտնվել'), 404
    execute('DELETE FROM improvement_actions WHERE id=?', (action_id,))
    return jsonify(ok=True)


@app.get('/api/admin/report')
@admin_required
def admin_report():
    schema = latest_schema()
    report = analyze([r for r in all_submissions() if r['surveyVersion'] == schema['version']], schema)
    report.pop('comments', None)
    return jsonify(report)


def safe_excel(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


@app.get('/api/admin/export.csv')
@admin_required
def admin_export():
    version = request.args.get('version', type=int)
    records = [r for r in all_submissions() if version is None or r['surveyVersion'] == version]
    return Response(export_rows_csv(records), content_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':'attachment; filename="apaga-evaluations.csv"'})



def redact(s):
    """Simple PII-minimization, not a guarantee of complete anonymization."""
    s = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', '[էլ. հասցե]', s)
    s = re.sub(r'(?:\+?\d[\d\s\-()]{8,}\d)', '[հեռախոսահամար]', s)
    return s[:700]


@app.post('/api/admin/ai')
@admin_required
def admin_ai():
    key = os.getenv('OPENAI_API_KEY', '')
    if not key:
        return jsonify(error='OPENAI_API_KEY-ը դեռ կարգավորված չէ։'), 503
    if rate_limited('ai', 20, 3600):
        return jsonify(error='Վերլուծության ժամային սահմանաչափը լրացել է։'), 429
    from openai import OpenAI
    schema = latest_schema()
    values = analyze([r for r in all_submissions() if r['surveyVersion'] == schema['version']], schema)
    if not values['responses']:
        return jsonify(error='Դեռ պատասխաններ չկան։'), 400
    comments = [redact(s) for s in values.pop('comments')[:160]]
    prompt = '''Դու Հայաստանի կրթական ծրագրերի գնահատման փորձագետ ես։ Ստեղծիր հայերեն, գործնական և չափելի խորքային զեկույց՝ հիմնվելով ԱՊԱԳԱ «Կրթությունից աշխատանք՝ մեկ քայլ» ծրագրի անանուն համախառն տվյալների վրա։
Ներառիր՝ 1) 5-7 տողանոց գործադիր ամփոփում, 2) դասընթացավարների համեմատություն՝ միայն տվյալներով, 3) 11 չափանիշների ուժեղ և թույլ կողմերը, 4) ուսանողների ազատ պատասխանների թեմատիկ կոդավորում, 5) իրագործելի 30/60/90 օրվա գործողությունների պլան, 6) տվյալների սահմանափակումներն ու վստահելիության նկատառումները։
Չենթադրես պատճառահետևանքային կապ՝ առանց ապացույցի։ Մի՛ հորինիր թվեր կամ մեջբերումներ։ Անձնական տվյալներ չկան. մի՛ փորձիր վերականգնել դրանք։'''
    client = OpenAI(api_key=key, timeout=55)
    try:
        result = client.responses.create(model=os.getenv('OPENAI_MODEL', 'gpt-4.1-mini'),
                                         instructions=prompt,
                                         input=json.dumps({'summary':values,'criteria':schema['criteria'],'anonymous_feedback':comments},ensure_ascii=False),
                                         max_output_tokens=2500)
        return jsonify(analysis=result.output_text)
    except Exception:
        app.logger.exception('OpenAI analysis failed')
        return jsonify(error='AI ծառայության հետ կապը չստացվեց։ Ստուգեք բանալին և մոդելի հասանելիությունը։'), 502


@app.post('/api/admin/email')
@admin_required
def admin_email():
    address = (request.get_json(silent=True) or {}).get('email', '')
    if not isinstance(address, str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', address) or len(address)>180:
        return jsonify(error='Ստացողի հասցեն սխալ է'), 400
    host, sender = os.getenv('SMTP_HOST'), os.getenv('SMTP_FROM')
    if not host or not sender:
        return jsonify(error='Էլեկտրոնային փոստի SMTP կարգավորումները բացակայում են։'), 503
    if rate_limited('mail', 20, 3600):
        return jsonify(error='Ուղարկումների սահմանաչափը լրացել է։'), 429
    schema = latest_schema()
    rpt = analyze([r for r in all_submissions() if r['surveyVersion'] == schema['version']], schema)
    lines = [f'ԱՊԱԳԱ. Դասընթացների գնահատման ամփոփում ({now()[:10]})', '',
             f"Պատասխաններ՝ {rpt['responses']}",f"Ընդհանուր միջին՝ {rpt['overall_average']}",f"Առաջարկների քանակ՝ {rpt['suggestions']}",'']
    for t in rpt['trainers']:
        lines.append(f"{t['name']}: միջին՝ {t['average']}; մասնակցություն՝ {t['count']}")
    lines.append('\nՏեսնել մանրամասները՝ կառավարման վահանակում։')
    msg=EmailMessage()
    msg['From']=sender
    msg['To']=address
    msg['Subject']='ԱՊԱԳԱ — դասընթացների գնահատման հաշվետվություն'
    msg.set_content('\n'.join(lines))
    try:
        port=int(os.getenv('SMTP_PORT','465'))
        if port==465:
            with smtplib.SMTP_SSL(host,port,context=ssl.create_default_context(),timeout=18) as smtp:
                if os.getenv('SMTP_USER'): smtp.login(os.getenv('SMTP_USER'),os.getenv('SMTP_PASSWORD',''))
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host,port,timeout=18) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if os.getenv('SMTP_USER'): smtp.login(os.getenv('SMTP_USER'),os.getenv('SMTP_PASSWORD',''))
                smtp.send_message(msg)
    except Exception:
        app.logger.exception('SMTP send failed')
        return jsonify(error='SMTP-ով ուղարկումը չհաջողվեց։ Ստուգեք կարգավորումները։'), 502
    return jsonify(ok=True)


init_db()

if __name__ == '__main__':
    app.run(host='127.0.0.1',port=int(os.getenv('PORT','5000')),debug=False)
