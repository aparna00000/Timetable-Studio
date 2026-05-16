import json
import copy
import random
from flask import Flask, render_template, request, redirect, url_for, send_file, jsonify, session, flash
from functools import wraps
import pandas as pd
import os
import io
from werkzeug.utils import secure_filename
import shutil
from io import BytesIO
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# PDF Generation
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

app = Flask(__name__)

# Database Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'timetable.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = "super_secret_mini_project_key_for_timetable"
db = SQLAlchemy(app)

# ──────────────────────────────────────────────
# DATABASE MODELS
# ──────────────────────────────────────────────
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    # Relationship to their timetable data
    timetable = db.relationship('TimetableData', backref='owner', lazy=True, uselist=False)

    def __init__(self, username, password):
        self.username = username
        self.password = password

class TimetableData(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False, default='{}')

    def __init__(self, user_id, content='{}'):
        self.user_id = user_id
        self.content = content

# Initialize Database
with app.app_context():
    db.create_all()

UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
SECTIONS_FILE = os.path.join(BASE_DIR, "sections_config.json")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.secret_key = "super_secret_mini_project_key_for_timetable" # Ensure sessions work


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated_function

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def cleanup_uploads():
    """Delete all files in the uploads folder to keep the server clean."""
    if os.path.exists(UPLOAD_FOLDER):
        for filename in os.listdir(UPLOAD_FOLDER):
            file_path = os.path.join(UPLOAD_FOLDER, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print(f"Failed to delete {file_path}. Reason: {e}")

def load_data():
    """Load the current user's timetable from the database."""
    user_id = session.get("user_id")
    if not user_id:
        return {}
    
    data_entry = TimetableData.query.filter_by(user_id=user_id).first()
    if data_entry:
        try:
            return json.loads(data_entry.content)
        except:
            return {}
    return {}

def save_data(data):
    """Save the current user's timetable to the database."""
    user_id = session.get("user_id")
    if not user_id:
        return
        
    data_entry = TimetableData.query.filter_by(user_id=user_id).first()
    if data_entry:
        data_entry.content = json.dumps(data)
    else:
        data_entry = TimetableData(user_id=user_id, content=json.dumps(data))
        db.session.add(data_entry)
            
    db.session.commit()

def load_lab_resources():
    paths = [os.path.join(BASE_DIR, "lab_resources.json"), os.path.join(PARENT_DIR, "lab_resources.json")]
    for path in paths:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    return {"department_rooms": {}, "shared_blocks": {}}

def load_sections():
    if os.path.exists(SECTIONS_FILE):
        with open(SECTIONS_FILE, "r") as f:
            return json.load(f)
    # Default: 1 section per dept per sem (e.g., CS1)
    return {}

def save_sections(data):
    with open(SECTIONS_FILE, "w") as f:
        json.dump(data, f, indent=4)

def get_sections_for(dept, sem):
    """Get sections list for a dept+sem. Falls back to ['Section 1']."""
    sections = load_sections()
    key = f"{dept}|{sem}"
    return sections.get(key, [f"{dept}1"])

# ──────────────────────────────────────────────
# Global storage is now handled per-request to avoid sync issues.
# ──────────────────────────────────────────────

# ══════════════════════════════════════════════
# HELPER: Iterate all timetables (dept → sem → section → schedule)
# ══════════════════════════════════════════════
def iter_all_timetables(all_tt):
    """Yields (dept, sem, section, schedule) for every timetable."""
    for dept, sems in all_tt.items():
        for sem, sections in sems.items():
            for section, schedule in sections.items():
                yield dept, sem, section, schedule

def is_teacher_free(teacher_name, day, period_idx, all_tt, skip_dept, skip_sem, skip_section, check_hectic=False):
    """Check if a teacher is available at a specific period globally (case-insensitive)."""
    if not teacher_name or str(teacher_name).lower() in ["free", "tba"]:
        return True
    
    t_name_low = str(teacher_name).lower().strip()
        
    # Global Collision Check
    for dept, sem, section, schedule in iter_all_timetables(all_tt):
        if dept == skip_dept and sem == skip_sem and section == skip_section:
            continue
        # Use .get() to avoid KeyError if day is missing
        day_schedule = schedule.get(day, [])
        if not day_schedule or period_idx >= len(day_schedule):
            continue
            
        slot = day_schedule[period_idx]
        if isinstance(slot, dict) and slot.get("teacher"):
            if str(slot["teacher"]).lower().strip() == t_name_low:
                return False
            
    return True

def is_room_free(room_name, day, period_start, all_tt, skip_dept, skip_sem, skip_section):
    """Check room availability across ALL sections globally."""
    if not room_name:
        return True
    num_periods = 6
    for dept, sem, section, schedule in iter_all_timetables(all_tt):
        if dept == skip_dept and sem == skip_sem and section == skip_section:
            continue
        for p in range(period_start, period_start + 3):
            if p >= num_periods:
                continue # Skip out of bounds periods instead of returning False
            slot = schedule[day][p]
            if isinstance(slot, dict) and slot.get("room") == room_name:
                return False
    return True

def make_empty_timetable():
    """Create a blank weekly timetable."""
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    return {
        day: [{"subject": "Free", "teacher": "Free", "type": "Free", "room": ""} for _ in range(6)]
        for day in days
    }

def ensure_timetable(all_tt, dept, sem, section):
    """Ensure a dept/sem/section timetable exists."""
    if dept not in all_tt:
        all_tt[dept] = {}
    if sem not in all_tt[dept]:
        all_tt[dept][sem] = {}
    if section not in all_tt[dept][sem]:
        all_tt[dept][sem][section] = make_empty_timetable()

def find_available_room(subject, dept, day, start_p, all_tt, skip_dept, skip_sem, skip_section):
    resources = load_lab_resources()
    subj = str(subject).lower().replace("lab", "").strip()
    
    # SPECIAL CASE: Project Lab (Uses regular classroom, no lab room needed)
    if "project" in subj:
        return f"{skip_section} Classroom"
    
    strict_keywords = ["physics", "phy", "chem", "che", "bcme", "workshop", "graph"]
    is_strict_subject = any(sk in subj for sk in strict_keywords)
    
    # STEP 1: Direct Name Match (Automatic & Specific)
    # If "Data Structure" is in the room name, use it!
    for d, rooms in resources.get("department_rooms", {}).items():
        for r in rooms:
            if subj in r.lower() and len(subj) > 2:
                if is_room_free(r, day, start_p, all_tt, skip_dept, skip_sem, skip_section):
                    return r
    
    # STEP 2: Department Labs (Priority fallback)
    # Use any available room in our own department before looking elsewhere.
    dept_rooms = resources.get("department_rooms", {}).get(dept, [])
    
    for r in dept_rooms:
        r_low = r.lower()
        if is_room_free(r, day, start_p, all_tt, skip_dept, skip_sem, skip_section):
            is_specialized_room = any(sk in r_low for sk in strict_keywords)
            
            # RULE: Specialized rooms (Phy Lab) only for matching subjects.
            # RULE: Specialized subjects (Chem) only for specialized rooms.
            if is_specialized_room:
                # If room is specialized, subject must match it
                if any(sk in r_low and sk in subj for sk in strict_keywords):
                    return r
            else:
                # If room is GENERIC (IT lab), subject must NOT be strict (Chem/Phy)
                if not is_strict_subject:
                    return r

    # RULE: 2nd, 3rd, and 4th years are ONLY allowed to use their own department labs.
    # Only First Year is allowed to use Common/Shared labs (Physics, Chem, etc.)
    if str(skip_sem).strip().lower() != "first year":
        return None

    # STEP 3: Common Subject Search (Building-wide search for things like Physics/Chem)
    # This is primarily for First Year common subjects.
    common_patterns = {
        "physics": ["physics", "phy"],
        "chem": ["chem", "chemistry", "che"],
        "bcme": ["bcme"],
        "workshop": ["workshop", "mechanical"],
        "electrical": ["ee", "electrical"],
        "electronics": ["ec", "electronics", "digit"],
        "computer": ["cs", "it", "computer", "program", "data", "structures", "coding", "ai"]
    }
    
    for key, aliases in common_patterns.items():
        if key in subj or any(a in subj for a in aliases):
            for d, rooms in resources.get("department_rooms", {}).items():
                for r in rooms:
                    if any(a in r.lower() for a in aliases):
                        if is_room_free(r, day, start_p, all_tt, skip_dept, skip_sem, skip_section):
                            return r

    # STEP 4: Shared Blocks (Last Resort for First Year)
    for block_name, rooms in resources.get("shared_blocks", {}).items():
        if dept.upper() in block_name.upper():
            for r in rooms:
                r_low = r.lower()
                if is_room_free(r, day, start_p, all_tt, skip_dept, skip_sem, skip_section):
                    is_specialized_room = any(sk in r_low for sk in strict_keywords)
                    if is_specialized_room:
                        if any(sk in r_low and sk in subj for sk in strict_keywords):
                            return r
                    else:
                        if not is_strict_subject:
                            return r

    return None

# ══════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            session["username"] = user.username
            flash(f"Welcome back, {user.username}!", "success")
            return redirect(url_for("home"))
        else:
            flash("Invalid username or password.", "danger")
            
    return render_template("login.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash("Username already exists. Please choose another.", "warning")
        else:
            hashed_pw = generate_password_hash(password)
            new_user = User(username=username, password=hashed_pw)
            db.session.add(new_user)
            db.session.commit()
            
            # Create empty timetable for new user
            new_data = TimetableData(user_id=new_user.id, content='{}')
            db.session.add(new_data)
            db.session.commit()
            
            # Auto-login the new user
            session["user_id"] = new_user.id
            session["username"] = new_user.username
            
            flash(f"Account created successfully! Welcome aboard, {new_user.username}.", "success")
            return redirect(url_for("home"))
            
    return render_template("signup.html")

@app.route("/logout")
def logout():
    session.pop("user_id", None)
    session.pop("username", None)
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))

@app.route("/create")
@login_required
def create_page():
    return render_template("create.html")

@app.route("/lab")
@login_required
def lab_page():
    resources = load_lab_resources()
    return render_template("lab.html", resources=resources)

@app.route("/view")
def view_page():
    all_tt = load_data()
    teachers = set()
    for dept, sem, section, schedule in iter_all_timetables(all_tt):
        for day_slots in schedule.values():
            for slot in day_slots:
                if isinstance(slot, dict) and slot.get("teacher"):
                    t_name = str(slot["teacher"]).strip()
                    if t_name.lower() not in ["free", "tba", "none", ""]:
                        # Normalize to Uppercase for the list to merge "ANU" and "anu"
                        teachers.add(t_name.upper())

    return render_template("view.html",
                           timetables=all_tt,
                           teachers=sorted(list(teachers)),
                           departments=sorted(list(all_tt.keys())))

@app.route("/get_lab_metadata")
def get_lab_metadata():
    all_tt = load_data()
    resources = load_lab_resources()
    departments = ["Applied Science", "CS", "IT", "MECH", "EE", "EC", "CIVIL"]
    for d in all_tt.keys():
        if d not in departments:
            departments.append(d)

    return {
        "departments": departments,
        "branch_depts": ["CS", "IT", "MECH", "EE", "EC", "CIVIL"],
        "rooms": resources,
        "years": ["First Year", "Second Year", "Third Year", "Fourth Year"],
        "sections_config": load_sections()
    }

@app.route("/save_sections", methods=["POST"])
@login_required
def save_sections_route():
    data = request.json
    save_sections(data)
    return jsonify({"status": "success"})

# ══════════════════════════════════════════════
# BULK LAB GENERATION (with sections + linked depts)
# ══════════════════════════════════════════════
@app.route("/generate_bulk_labs", methods=["POST"])
@login_required
def generate_bulk_labs():
    all_tt = load_data()
    payload = request.json
    entries = payload.get("entries", [])

    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    num_periods = 6
    start_positions = [0, 3]

    failed_placements = []
    success_count = 0

    for entry in entries:
        dept = entry.get("dept")
        sem = entry.get("sem")
        section = entry.get("section", f"{dept}1")
        avoid_day = entry.get("avoid")
        linked_depts = entry.get("linked_depts", [])  # Changed from linked_entries

        if not dept or not sem:
            continue

        ensure_timetable(all_tt, dept, sem, section)
        timetable = all_tt[dept][sem][section]
        labs = [entry.get("lab1"), entry.get("lab2")]

        placed_days = []
        # Alternate morning (0=P1-P3) and evening (3=P4-P6) for each lab
        next_session = random.choice([0, 3])  # First lab gets a random session

        for lab in labs:
            # Skip incomplete entries; room is optional now
            if not lab or not lab.get("subject"):
                continue

            lab_code = lab["subject"]
            # Room may be missing; handle later
            room = lab.get("room")
            teacher = lab.get("teacher", "TBA")

            placed = False

            search_days = list(days)
            random.shuffle(search_days)
            # Force this lab into the assigned session (morning or evening)
            search_positions = [next_session]

            for day in search_days:
                if placed:
                    break
                if day == avoid_day or day in placed_days:
                    continue

                for start_p in search_positions:
                    # Check own slots
                    slots_free = all(timetable[day][p]["subject"] == "Free" for p in range(start_p, start_p + 3))
                    # Determine room: use provided or auto-allocate
                    target_room = room
                    if not target_room or not is_room_free(target_room, day, start_p, all_tt, dept, sem, section):
                        auto_room = find_available_room(lab_code, dept, day, start_p, all_tt, dept, sem, section)
                        if auto_room:
                            target_room = auto_room
                        else:
                            continue
                    room_free = True  # We have ensured a free room in target_room
                    # Check global teacher availability
                    teacher_free = all(
                        is_teacher_free(teacher, day, p, all_tt, dept, sem, section)
                        for p in range(start_p, start_p + 3)
                    )

                    # Check linked sections are free
                    linked_free = True
                    for ld in linked_depts:
                        ls = f"{ld}1" # Default to Section 1 for linked branches
                        ensure_timetable(all_tt, ld, sem, ls)
                        linked_tt = all_tt[ld][sem][ls]
                        if not all(linked_tt[day][p]["subject"] == "Free" for p in range(start_p, start_p + 3)):
                            linked_free = False
                            break

                    if slots_free and room_free and teacher_free and linked_free:
                        slot_data = {"subject": lab_code, "teacher": teacher, "type": "Lab", "room": target_room}
                        for p in range(start_p, start_p + 3):
                            timetable[day][p] = slot_data.copy()
                        placed = True
                        placed_days.append(day)
                        success_count += 1
                        # Alternate: next lab gets the opposite session
                        next_session = 3 if next_session == 0 else 0

                        # Mirror to linked sections
                        if linked_depts:
                            mirror = {
                                "subject": lab_code, "teacher": teacher,
                                "type": "Lab", "room": target_room,
                                "linked": True, "linked_from": dept
                            }
                            for ld in linked_depts:
                                ls = f"{ld}1"
                                for p in range(start_p, start_p + 3):
                                    all_tt[ld][sem][ls][day][p] = mirror.copy()
                        break
                
                # If preferred session (morning/evening) failed, try the OTHER one to prevent skipping
                if not placed:
                    other_session = 3 if next_session == 0 else 0
                    for day in search_days:
                        if placed: break
                        if day == avoid_day or day in placed_days: continue
                        
                        start_p = other_session
                        # Repeat checks...
                        slots_free = all(timetable[day][p]["subject"] == "Free" for p in range(start_p, start_p + 3))
                        target_room = room
                        if not target_room or not is_room_free(target_room, day, start_p, all_tt, dept, sem, section):
                            auto_room = find_available_room(lab_code, dept, day, start_p, all_tt, dept, sem, section)
                            if auto_room: target_room = auto_room
                            else: continue
                        teacher_free = all(is_teacher_free(teacher, day, p, all_tt, dept, sem, section) for p in range(start_p, start_p + 3))
                        linked_free = True
                        for ld in linked_depts:
                            ls = f"{ld}1"
                            ensure_timetable(all_tt, ld, sem, ls)
                            if not all(all_tt[ld][sem][ls][day][p]["subject"] == "Free" for p in range(start_p, start_p + 3)):
                                linked_free = False; break
                                
                        if slots_free and teacher_free and linked_free:
                            slot_data = {"subject": lab_code, "teacher": teacher, "type": "Lab", "room": target_room}
                            for p in range(start_p, start_p + 3): timetable[day][p] = slot_data.copy()
                            placed = True; placed_days.append(day); success_count += 1
                            # We don't flip next_session here to keep the pattern for the next actual lab
                            if linked_depts:
                                mirror = {"subject": lab_code, "teacher": teacher, "type": "Lab", "room": target_room, "linked": True, "linked_from": dept}
                                for ld in linked_depts:
                                    ls = f"{ld}1"
                                    for p in range(start_p, start_p + 3): all_tt[ld][sem][ls][day][p] = mirror.copy()
                            break

            if not placed:
                failed_placements.append(f"{lab_code} ({teacher}) for {dept} {sem} {section}")

    save_data(all_tt)

    if failed_placements:
        return jsonify({
            "status": "partial",
            "message": f"{success_count} labs placed. {len(failed_placements)} could not be placed.",
            "failed": failed_placements
        })

    return jsonify({"status": "success", "message": f"All {success_count} labs allocated successfully!"})

@app.route("/save_lab_resources", methods=["POST"])
@login_required
def save_lab_resources():
    data = request.json
    with open(os.path.join(BASE_DIR, "lab_resources.json"), "w") as f:
        json.dump(data, f, indent=4)
    return {"status": "success"}

@app.route("/clear_data", methods=["POST"])
@login_required
def clear_data():
    all_tt = load_data()
    payload = request.json
    scope = payload.get("scope", "all")
    dept = payload.get("dept")
    sem = payload.get("sem")
    section = payload.get("section")

    if scope == "all":
        all_tt = {}
        save_data(all_tt)
        return jsonify({"status": "success", "message": "All timetable data cleared."})
    
    elif scope == "dept" and dept:
        if dept in all_tt:
            if sem:
                if sem in all_tt[dept]:
                    if section and section in all_tt[dept][sem]:
                        del all_tt[dept][sem][section]
                        if not all_tt[dept][sem]:
                            del all_tt[dept][sem]
                        if not all_tt[dept]:
                            del all_tt[dept]
                        save_data(all_tt)
                        return jsonify({"status": "success", "message": f"Cleared {dept} - {sem} - {section}."})
                    else:
                        # Clear entire semester if no section specified OR if section specified but not found
                        del all_tt[dept][sem]
                        if not all_tt[dept]:
                            del all_tt[dept]
                        save_data(all_tt)
                        return jsonify({"status": "success", "message": f"Cleared {dept} - {sem}."})
                else:
                    return jsonify({"status": "error", "message": f"Year '{sem}' not found in {dept}."})
            else:
                # Clear entire department
                del all_tt[dept]
                save_data(all_tt)
                return jsonify({"status": "success", "message": f"Cleared entire {dept} department."})
        else:
            return jsonify({"status": "error", "message": f"Department '{dept}' not found."})
            
    return jsonify({"status": "error", "message": "Nothing to clear."})

# ══════════════════════════════════════════════
# EXCEL UPLOAD (Theory + Lab from files)
# ══════════════════════════════════════════════
@app.route("/generate", methods=["POST"])
@login_required
def generate():
    all_tt = load_data()

    dept_choice = request.form.get("dept_dropdown", "General").strip()
    dept_custom = request.form.get("dept_custom", "").strip()
    department_name = dept_custom if dept_choice == "Other" else dept_choice

    year_name = request.form.get("year_name", "Unnamed Year").strip()
    section_name = request.form.get("section_name", f"{department_name}1").strip()
    teacher_file = request.files.get("teacher_file")
    lab_file = request.files.get("lab_file")

    teacher_data = []
    lab_data_all = []
    
    # 1. Handle Teacher/Theory Data
    theory_json = request.form.get("theory_data_json")
    if theory_json:
        try:
            teacher_data = json.loads(theory_json)
            print(f"Manual Editor: Received {len(teacher_data)} theory rows")
        except:
            pass
            
    if not teacher_data and teacher_file and teacher_file.filename != '':
        t_filename = secure_filename(teacher_file.filename)
        teacher_path = os.path.join(app.config["UPLOAD_FOLDER"], f"raw_{department_name}_{t_filename}")
        teacher_file.save(teacher_path)
        try:
            print(f"\n--- [GENERATE] Processing Upload for {department_name} ---")
            teacher_df = pd.read_excel(teacher_path)
            teacher_data = teacher_df.to_dict(orient="records")
            print(f"Teacher File: {len(teacher_df)} rows")
        except Exception as e:
            return f"Error reading teacher file: {str(e)}"

    if not teacher_data:
        return "Error: No teacher data provided (via upload or editor)."

    # 2. Handle Lab Data
    lab_json = request.form.get("lab_data_json")
    if lab_json:
        try:
            lab_data_all = json.loads(lab_json)
            print(f"Manual Editor: Received {len(lab_data_all)} lab rows")
        except:
            pass
            
    if not lab_data_all and lab_file and lab_file.filename != '':
        l_filename = secure_filename(lab_file.filename)
        lab_path = os.path.join(app.config["UPLOAD_FOLDER"], f"raw_lab_{department_name}_{l_filename}")
        lab_file.save(lab_path)
        try:
            lab_df = pd.read_excel(lab_path)
            lab_data_all = lab_df.to_dict(orient="records")
        except:
            pass

    try:
        # Create a DataFrame for column detection if teacher_data was manual
        teacher_df = pd.DataFrame(teacher_data)
        
        # Identify if this is a Bulk Upload (Multiple years in one file)
        is_bulk_request = request.form.get("bulk_mode") == "on"
        print(f"Bulk Mode Toggle: {is_bulk_request}")
            
        year_col = None
        for c in teacher_df.columns:
            cleaned_c = str(c).lower().strip().replace(" ", "").replace("/", "").replace("_", "")
            if cleaned_c in ["year", "semester", "sem", "yr", "class", "classdivision"]:
                year_col = c
                break
        
        if is_bulk_request or year_col:
            # Mapping S1, S2 -> First Year, etc.
            sem_map = {
                # Semester codes (S1-S8): two semesters per year
                "S1": "First Year",  "S2": "First Year",
                "S3": "Second Year", "S4": "Second Year",
                "S5": "Third Year",  "S6": "Third Year",
                "S7": "Fourth Year", "S8": "Fourth Year",
                # Direct year numbers (1-4): one number = one year
                "1": "First Year",
                "2": "Second Year",
                "3": "Third Year",
                "4": "Fourth Year",
                # Full text
                "FIRSTYEAR": "First Year",  "FIRST YEAR": "First Year",
                "SECONDYEAR": "Second Year", "SECOND YEAR": "Second Year",
                "THIRDYEAR": "Third Year",   "THIRD YEAR": "Third Year",
                "FOURTHYEAR": "Fourth Year", "FOURTH YEAR": "Fourth Year",
                # Common abbreviations
                "FY": "First Year", "SY": "Second Year",
                "TY": "Third Year", "LY": "Fourth Year",
            }

            if not year_col:
                return "Error: Bulk Mode active but no 'Year' or 'Semester' column found."

            unique_years = teacher_df[year_col].unique()
            success_list = []
            total_labs_placed = 0
            total_labs_skipped = 0

            for y in unique_years:
                raw_y = str(y).strip()
                if not raw_y or raw_y.lower() == 'nan': continue
                
                # Try to map raw_y to a year name (e.g., S1 -> First Year)
                # First clean the input: "Sem 1" -> "S1" or just "1"
                cleaned_raw_y = raw_y.upper().replace("SEM", "").replace("ESTER", "").replace(" ", "")
                y_str = sem_map.get(cleaned_raw_y, sem_map.get(raw_y.upper(), raw_y))
                
                print(f"  > Processing Year: {raw_y} -> {y_str}")
                
                t_subset = teacher_df[teacher_df[year_col] == y].to_dict(orient="records")
                l_subset = []
                for l_row in lab_data_all:
                    l_y_raw = str(get_col(l_row, "year", "semester") or "").strip().upper()
                    l_y_clean = l_y_raw.replace("SEM", "").replace("ESTER", "").replace(" ", "")
                    
                    # Match if raw value, cleaned value, or mapped value matches
                    if l_y_raw == raw_y.upper() or l_y_clean == cleaned_raw_y or l_y_raw == y_str.upper():
                        l_subset.append(l_row)
                
                sec = f"{department_name}1"
                new_tt = generate_timetable(t_subset, l_subset, all_tt, department_name, y_str, sec)
                
                if "lab_summary" in new_tt:
                    total_labs_placed += new_tt["lab_summary"]["placed"]
                    total_labs_skipped += new_tt["lab_summary"]["skipped"]
                    del new_tt["lab_summary"]

                ensure_timetable(all_tt, department_name, y_str, sec)
                all_tt[department_name][y_str][sec] = new_tt
                success_list.append(y_str)
            
            save_data(all_tt)
            lab_msg = f" | {total_labs_placed} Labs placed, {total_labs_skipped} skipped." if total_labs_placed + total_labs_skipped > 0 else ""
            flash(f"Generated bulk timetables for {', '.join(set(success_list))}.{lab_msg}", "success")
        else:
            # ── SINGLE CLASS MODE ──
            year_name = request.form.get("year_name", "Unnamed Year").strip()
            section_name = request.form.get("section_name", f"{department_name}1").strip()
            
            new_timetable = generate_timetable(teacher_data, lab_data_all, all_tt, department_name, year_name, section_name)
            
            lab_msg = ""
            if "lab_summary" in new_timetable:
                lab_msg = f" ({new_timetable['lab_summary']['placed']} Labs placed, {new_timetable['lab_summary']['skipped']} skipped)"
                del new_timetable["lab_summary"]

            ensure_timetable(all_tt, department_name, year_name, section_name)
            all_tt[department_name][year_name][section_name] = new_timetable
            save_data(all_tt)
            flash(f"Generated timetable for {department_name} - {year_name}.{lab_msg}", "success")

        # Cleanup uploaded files after processing
        cleanup_uploads()
        return redirect(url_for("view_page"))
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return f"Error processing files: {str(e)}"

# ══════════════════════════════════════════════
# SLOT SWAP (Edit timetable from view page)
# ══════════════════════════════════════════════
@app.route("/swap_slots", methods=["POST"])
@login_required
def swap_slots():
    all_tt = load_data()
    payload = request.json
    dept = payload.get("dept")
    sem = payload.get("sem")
    section = payload.get("section")
    
    day1 = payload.get("day1")
    idx1 = int(payload.get("idx1"))
    day2 = payload.get("day2")
    idx2 = int(payload.get("idx2"))
    
    try:
        schedule = all_tt[dept][sem][section]
        slot1 = schedule[day1][idx1]
        slot2 = schedule[day2][idx2]
        
        # Swap
        schedule[day1][idx1] = slot2
        schedule[day2][idx2] = slot1
        
        save_data(all_tt)
        return jsonify({"status": "success", "message": f"Swapped {day1} P{idx1+1} ↔ {day2} P{idx2+1}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

# ══════════════════════════════════════════════
# DOWNLOAD
# ══════════════════════════════════════════════
@app.route("/download/<type>/<path:value>")
def download(type, value):
    all_tt = load_data()
    output = io.BytesIO()
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    periods = ["P1", "P2", "P3", "P4", "P5", "P6"]

    data = []
    friendly_name = "timetable"

    if type == "class":
        try:
            parts = value.split("/")
            dept, sem, section = parts[0], parts[1], parts[2]
            sec_data = all_tt[dept][sem][section]
            friendly_name = f"{section} {sem} Timetable"
            for day in days:
                row = {"Day": day}
                for i, p in enumerate(periods):
                    slot = sec_data[day][i]
                    if isinstance(slot, dict) and slot.get("subject") != "Free":
                        room_info = f" [{slot.get('room', '')}]" if slot.get('room') else ""
                        row[p] = f"{slot['subject']}{room_info} ({slot['teacher']})"
                    else:
                        row[p] = "Free"
                data.append(row)
        except Exception as e:
            return f"Error: {str(e)}"

    elif type == "teacher":
        friendly_name = f"{value} Timetable"
        for day in days:
            row = {"Day": day}
            for i, p in enumerate(periods):
                classes_at_this_period = []
                for dept, sem, section, schedule in iter_all_timetables(all_tt):
                    slot = schedule[day][i]
                    if isinstance(slot, dict) and slot.get("teacher"):
                        if str(slot["teacher"]).lower().strip() == str(value).lower().strip():
                            room_info = f" [{slot.get('room', '')}]" if slot.get('room') else ""
                            class_info = f"{slot['subject']}{room_info} ({dept}-{sem}-{section})"
                            if class_info not in classes_at_this_period:
                                classes_at_this_period.append(class_info)
                
                if classes_at_this_period:
                    row[p] = " / ".join(classes_at_this_period)
                else:
                    row[p] = "Free"
            data.append(row)

    if not data:
        return "No data found for the requested timetable."

    df = pd.DataFrame(data)
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Timetable')

    output.seek(0)
    filename = value.replace("/", "_")
    return send_file(output, as_attachment=True, download_name=f"Timetable_{filename}.xlsx")

# ══════════════════════════════════════════════
# COLUMN HELPER
# ══════════════════════════════════════════════
def get_col(row, *possible_names):
    """Helper to find column values ignoring case and spaces"""
    for col in row.keys():
        cleaned_col = str(col).lower().replace(" ", "").replace("/", "").replace("_", "")
        for poss in possible_names:
            cleaned_poss = poss.lower().replace(" ", "").replace("/", "").replace("_", "")
            if cleaned_col == cleaned_poss:
                val = row[col]
                if pd.isna(val):
                    return None
                if isinstance(val, str):
                    return val.strip()
                return val
    return None

# ══════════════════════════════════════════════
# 🧠 TIMETABLE GENERATION ENGINE
# ══════════════════════════════════════════════
def generate_timetable(teachers, labs, existing_timetables, current_dept, current_sem, current_section):
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    num_periods = 6

    # Load existing timetable or create fresh
    timetable = None
    if current_dept in existing_timetables:
        if current_sem in existing_timetables[current_dept]:
            if current_section in existing_timetables[current_dept][current_sem]:
                timetable = copy.deepcopy(existing_timetables[current_dept][current_sem][current_section])

    if not timetable:
        timetable = make_empty_timetable()

    # 🔹 Step 1: Place Labs
    placed_lab_days = []  # Track days used by labs to enforce different days
    next_lab_session = random.choice([0, 3])  # Alternate: 0=morning (P1-P3), 3=evening (P4-P6)

    for lab in labs:
        try:
            day = get_col(lab, "day")
            start_val = get_col(lab, "startperiod", "start")
            
            teacher = get_col(lab, "teachername", "teacher") or "TBA"
            subject = get_col(lab, "labsubject", "subject", "labsub") or "Lab"
            room = get_col(lab, "room", "labroom")
            
            auto_allocate = not day # If day is missing, find one automatically
            
            placed = False
            search_days = [day] if not auto_allocate else ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
            if auto_allocate: random.shuffle(search_days)
            
            # If auto-allocating, use the alternating session; otherwise use what user specified
            if start_val and not auto_allocate:
                search_positions = [int(start_val)-1]
            else:
                search_positions = [next_lab_session]

            for d in search_days:
                if placed: break
                
                # Normalize day name
                day_str = str(d).strip().lower()
                day_map = {
                    "monday": "Monday", "mon": "Monday", "m0nday": "Monday",
                    "tuesday": "Tuesday", "tue": "Tuesday", "tues": "Tuesday",
                    "wednesday": "Wednesday", "wed": "Wednesday",
                    "thursday": "Thursday", "thu": "Thursday", "thur": "Thursday", "thusrday": "Thursday",
                    "friday": "Friday", "fri": "Friday"
                }
                actual_day = day_map.get(day_str, str(d).title())
                if actual_day not in days: continue
                
                # Enforce different days for auto-allocated labs
                if auto_allocate and actual_day in placed_lab_days:
                    continue

                for start_p in search_positions:
                    end_p = min(start_p + 3, num_periods)
                    
                    # 1. Check if OWN slots are free
                    if not all(timetable[actual_day][p]["subject"] == "Free" for p in range(start_p, end_p)):
                        continue
                        
                    # 2. Room Allocation
                    target_room = room
                    if not target_room or not is_room_free(target_room, actual_day, start_p, existing_timetables, current_dept, current_sem, current_section):
                        # Try to find another room if unspecified or busy
                        auto_room = find_available_room(subject, current_dept, actual_day, start_p, existing_timetables, current_dept, current_sem, current_section)
                        if auto_room:
                            target_room = auto_room
                        elif not target_room: # No room provided and none found
                            continue
                    
                    # 3. Final Validation (Teacher + Room)
                    can_use_this_slot = True
                    if teacher != "TBA":
                        if not all(is_teacher_free(teacher, actual_day, p, existing_timetables, current_dept, current_sem, current_section) for p in range(start_p, end_p)):
                            can_use_this_slot = False
                    
                    if can_use_this_slot and target_room:
                        if is_room_free(target_room, actual_day, start_p, existing_timetables, current_dept, current_sem, current_section):
                            # SUCCESS: Place the lab
                            for i in range(start_p, end_p):
                                timetable[actual_day][i] = {"subject": subject, "teacher": teacher, "type": "Lab", "room": target_room}
                            
                            if "lab_summary" not in timetable: timetable["lab_summary"] = {"placed": 0, "skipped": 0, "details": []}
                            timetable["lab_summary"]["placed"] += 1
                            placed = True
                            placed_lab_days.append(actual_day)
                            # Alternate session for next lab
                            next_lab_session = 3 if next_lab_session == 0 else 0
                            break

            if not placed:
                if "lab_summary" not in timetable: timetable["lab_summary"] = {"placed": 0, "skipped": 0, "details": []}
                timetable["lab_summary"]["skipped"] += 1
                timetable["lab_summary"]["details"].append(f"{subject}")
                print(f"⚠️ Could not allocate lab '{subject}' automatically.")
                
        except Exception as e:
            print("Lab placement error:", e)
            continue
    
    # 🔹 Step 2: Place Theory subjects
    def get_free_slots():
        slots = []
        for day in days:
            for i in range(num_periods):
                if timetable[day][i]["subject"] == "Free":
                    slots.append((day, i))
        random.shuffle(slots)
        return slots

    def is_adjacent_same_teacher_or_subject(teacher, subject, day, period_idx):
        t_low = str(teacher).lower().strip()
        s_low = str(subject).lower().strip()
        for adj in [period_idx - 1, period_idx + 1]:
            if 0 <= adj < num_periods:
                slot = timetable[day][adj]
                if isinstance(slot, dict) and slot.get("type") == "Theory":
                    if str(slot.get("teacher")).lower().strip() == t_low or \
                       str(slot.get("subject")).lower().strip() == s_low:
                        return True
        return False
        
    def has_subject_on_day(subject, day):
        s_low = str(subject).lower().strip()
        for i in range(num_periods):
            slot = timetable[day][i]
            if isinstance(slot, dict) and str(slot.get("subject")).lower().strip() == s_low:
                return True
        return False

    shuffled_teachers = list(teachers)
    random.shuffle(shuffled_teachers)

    for subject_info in shuffled_teachers:
        type_val = str(get_col(subject_info, "type(theorylab)", "type") or "theory").strip().lower()

        if type_val == "theory":
            hours_val = get_col(subject_info, "hoursperweek", "hours", "hourperweek")
            try:
                hours = int(float(str(hours_val).strip())) if pd.notna(hours_val) and str(hours_val).strip() else 0
            except ValueError:
                hours = 0

            name = get_col(subject_info, "subjectname", "subject") or "Subject"
            teacher = get_col(subject_info, "teachername", "teacher") or "TBA"

            # Pass 1: Try to place only 1 period per day for this subject
            free_slots = get_free_slots()
            for (day, i) in free_slots:
                if hours <= 0:
                    break
                if timetable[day][i]["subject"] != "Free":
                    continue
                if not has_subject_on_day(name, day):
                    if is_teacher_free(teacher, day, i, existing_timetables, current_dept, current_sem, current_section):
                        if not is_adjacent_same_teacher_or_subject(teacher, name, day, i):
                            timetable[day][i] = {"subject": name, "teacher": teacher, "type": "Theory"}
                            hours -= 1

            # Pass 2: If hours still remain (e.g., more than 5 hours per week), 
            # place multiple per day but STRICTLY enforce no consecutive periods.
            if hours > 0:
                free_slots = get_free_slots()
                for (day, i) in free_slots:
                    if hours <= 0:
                        break
                    if timetable[day][i]["subject"] != "Free":
                        continue
                    if is_teacher_free(teacher, day, i, existing_timetables, current_dept, current_sem, current_section):
                        if not is_adjacent_same_teacher_or_subject(teacher, name, day, i):
                            timetable[day][i] = {"subject": name, "teacher": teacher, "type": "Theory"}
                            hours -= 1

    return timetable

@app.route("/download/pdf/<type>/<path:value>")
def download_pdf(type, value):
    all_tt = load_data()
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    periods = ["P1", "P2", "P3", "P4", "P5", "P6"]
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    elements = []
    styles = getSampleStyleSheet()
    
    # Custom Styles
    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], alignment=1, fontSize=18, spaceAfter=20)
    table_cell_style = ParagraphStyle('TableCell', parent=styles['Normal'], alignment=1, fontSize=8)
    
    title_text = ""
    table_data = [["Day"] + periods]
    
    if type == "class":
        try:
            dept, sem, section = value.split("/")
            title_text = f"Timetable: {dept} - {sem} - {section}"
            schedule = all_tt.get(dept, {}).get(sem, {}).get(section, {})
            for day in days:
                row = [day]
                for i in range(6):
                    slot = schedule.get(day, [None]*6)[i]
                    if isinstance(slot, dict) and slot.get("subject") and slot["subject"] != "Free":
                        room_info = f"<br/>[{slot.get('room', '')}]" if slot.get('room') else ""
                        txt = f"<b>{slot['subject']}</b><br/>{slot['teacher']}{room_info}"
                        row.append(Paragraph(txt, table_cell_style))
                    else:
                        row.append("Free")
                table_data.append(row)
        except:
            return "Error: Invalid class value"
            
    elif type == "teacher":
        title_text = f"Teacher Timetable: {value.upper()}"
        for day in days:
            row = [day]
            for i in range(6):
                classes = []
                for dept, sem, sec, schedule in iter_all_timetables(all_tt):
                    # Safe access to schedule days
                    day_schedule = schedule.get(day, [None]*6)
                    slot = day_schedule[i]
                    if isinstance(slot, dict) and slot.get("teacher") and slot["teacher"].lower().strip() == value.lower().strip():
                        room_info = f" [{slot.get('room', '')}]" if slot.get('room') else ""
                        classes.append(f"{slot['subject']}{room_info}<br/>({dept}-{sem}-{sec})")
                
                if classes:
                    row.append(Paragraph(" / ".join(list(set(classes))), table_cell_style))
                else:
                    row.append("Free")
            table_data.append(row)

    # Build PDF Elements
    elements.append(Paragraph(title_text, title_style))
    elements.append(Spacer(1, 0.2 * inch))
    
    # Style the table
    t = Table(table_data, colWidths=[1*inch] + [1.4*inch]*6)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.whitesmoke),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 1), (0, -1), colors.whitesmoke),
    ]))
    elements.append(t)
    
    doc.build(elements)
    buffer.seek(0)
    
    # Create a user-friendly filename
    if type == "class":
        parts = value.split("/")
        # e.g., "CS1 First Year Timetable.pdf"
        friendly_name = f"{parts[2]} {parts[1]} Timetable"
    else:
        friendly_name = f"{value} Timetable"
        
    return send_file(buffer, as_attachment=True, download_name=f"{friendly_name}.pdf", mimetype='application/pdf')

if __name__ == "__main__":
    app.run(debug=True)
