from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session
import firebase_admin
from firebase_admin import credentials, firestore, auth
from functools import wraps
import io
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
import os

# Add this function near the top of your file, after imports
def convert_firebase_timestamp(timestamp_value):
    """
    Convert any Firebase timestamp format to a Python datetime.
    Handles:
    - DatetimeWithNanoseconds objects (from SERVER_TIMESTAMP) 
    - Float/integer timestamps (from timestamp())
    - Already converted datetime objects
    - None values
    
    Returns timezone-naive datetime objects.
    """
    if timestamp_value is None:
        return None
        
    # If it's already a datetime
    if isinstance(timestamp_value, datetime):
        return timestamp_value.replace(tzinfo=None) if timestamp_value.tzinfo else timestamp_value
        
    # If it's a DatetimeWithNanoseconds object
    try:
        dt = timestamp_value.datetime()
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except AttributeError:
        pass
        
    # If it's a float/integer timestamp
    try:
        if isinstance(timestamp_value, (int, float)):
            dt = datetime.fromtimestamp(timestamp_value)
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except (TypeError, ValueError, OSError, OverflowError):
        pass
        
    # Return original if conversion fails
    return timestamp_value

def make_timezone_naive(obj):
    """
    Recursively process a data structure (dict, list, etc.) and convert 
    all datetime objects to timezone-naive.
    """
    if isinstance(obj, dict):
        return {k: make_timezone_naive(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_timezone_naive(item) for item in obj]
    elif isinstance(obj, datetime):
        return obj.replace(tzinfo=None) if obj.tzinfo else obj
    elif hasattr(obj, 'datetime'):
        # Handle Firebase server timestamps with datetime() method
        try:
            dt = obj.datetime()
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except (AttributeError, TypeError):
            return obj
    elif isinstance(obj, (int, float)) and obj > 946684800:  # Jan 1, 2000 timestamp
        # Might be a timestamp stored as float/int - check if reasonable date
        try:
            dt = datetime.fromtimestamp(obj)
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except (ValueError, OSError, OverflowError):
            return obj
    else:
        return obj

def sort_students_by_roll(students_data):
    """Sort students by roll number"""
    return sorted(students_data, key=lambda x: x.get('roll_number', ''))

# Initialize Flask app
app = Flask(__name__)
load_dotenv()  # Load environment variables from .env file
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'default-secret-key')

# Initialize Firebase
cred = credentials.Certificate("firebase.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.context_processor
def inject_datetime():
    return dict(datetime=datetime)

@app.context_processor
def inject_user():
    return {
        'current_user': {
            'is_authenticated': 'user_id' in session,
            'username': session.get('username', ''),
            'name': session.get('name', '')
        }
    }

# Routes
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        try:
            # Get teacher by username from Firestore
            teachers_ref = db.collection('teachers')
            query = teachers_ref.where('username', '==', username).limit(1).get()
            
            if not query:
                error = 'Invalid username or password'
                return render_template('login.html', error=error)
                
            teacher_data = next(iter(query), None)
            
            if not teacher_data:
                error = 'Invalid username or password'
                return render_template('login.html', error=error)
            
            # Get the Firebase Auth UID from teacher document
            teacher_dict = teacher_data.to_dict()
            firebase_uid = teacher_dict.get('firebase_uid')
            email = teacher_dict.get('email')
            
            # Sign in with Firebase Authentication
            try:
                user = auth.get_user(firebase_uid)
                session['user_id'] = firebase_uid
                session['username'] = username
                session['name'] = teacher_dict.get('name', '')
                session['teacher_id'] = teacher_data.id
                
                return redirect(url_for('dashboard'))
            except Exception as auth_error:
                print(f"Firebase Auth error: {auth_error}")
                error = 'Authentication failed'
        
        except Exception as e:
            print(f"Login error: {e}")
            error = 'Login failed. Please try again.'
    
    return render_template('login.html', error=error)

@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        name = request.form.get('name')
        email = request.form.get('email')
        
        try:
            # Check if username already exists
            existing_users = db.collection('teachers').where('username', '==', username).get()
            if len(list(existing_users)) > 0:
                error = 'Username already exists'
                return render_template('register.html', error=error)
            
            # Create user in Firebase Authentication
            user = auth.create_user(
                email=email,
                password=password,
                display_name=name
            )
            
            # Store teacher data in Firestore
            teacher_data = {
                'username': username,
                'name': name,
                'email': email,
                'firebase_uid': user.uid,
                'created_at': firestore.SERVER_TIMESTAMP
            }
            
            db.collection('teachers').add(teacher_data)
            
            flash('Registration successful! Please login.')
            return redirect(url_for('login'))
            
        except Exception as e:
            print(f"Registration error: {e}")
            error = 'Registration failed. Please try again.'
    
    return render_template('register.html', error=error)

@app.route('/dashboard')
@login_required
def dashboard():
    teacher_id = session.get('teacher_id')
    
    # Count students
    students_ref = db.collection('students').where('teacher_id', '==', teacher_id)
    students_count = len(list(students_ref.get()))
    
    # Count assignments
    assignments_ref = db.collection('assignments').where('teacher_id', '==', teacher_id)
    assignments_count = len(list(assignments_ref.get()))
    
    # Get recent assignments
    recent_assignments = []
    assignments_query = assignments_ref.order_by('created_at', direction=firestore.Query.DESCENDING).limit(5).get()
    
    for doc in assignments_query:
        assignment = {'id': doc.id, **doc.to_dict()}
        
        # Convert timestamps properly
        if 'due_date' in assignment and assignment['due_date']:
            # Handle the case where due_date is stored as a timestamp (float)
            try:
                assignment['due_date'] = datetime.fromtimestamp(assignment['due_date'])
            except (TypeError, ValueError):
                # If it's already a datetime or a different format, keep it as is
                pass
        
        if 'created_at' in assignment and assignment['created_at']:
            # Handle server timestamp which returns a DatetimeWithNanoseconds object
            try:
                assignment['created_at'] = assignment['created_at'].datetime()
            except AttributeError:
                # If it's not a DatetimeWithNanoseconds object, try to convert it
                try:
                    if isinstance(assignment['created_at'], (int, float)):
                        assignment['created_at'] = datetime.fromtimestamp(assignment['created_at'])
                except (TypeError, ValueError):
                    pass
        
        recent_assignments.append(assignment)
    
    return render_template('dashboard.html', 
                          students_count=students_count, 
                          assignments_count=assignments_count,
                          recent_assignments=recent_assignments,
                          now=datetime.now())

@app.route('/students')
@login_required
def students():
    teacher_id = session.get('teacher_id')
    
    # Get all students for this teacher
    students_ref = db.collection('students').where('teacher_id', '==', teacher_id)
    students_data = [{'id': doc.id, **doc.to_dict()} for doc in students_ref.get()]
    
    # Sort students by roll number
    students_data = sort_students_by_roll(students_data)
    
    return render_template('students.html', students=students_data)

@app.route('/add_student', methods=['GET', 'POST'])
@login_required
def add_student():
    if request.method == 'POST':
        name = request.form.get('name')
        roll_number = request.form.get('roll_number')
        department = request.form.get('department')
        class_name = request.form.get('class_name')
        school = request.form.get('school')
        phone_number = request.form.get('phone_number')
        email = request.form.get('email')
        teacher_id = session.get('teacher_id')
        
        new_student = {
            'name': name,
            'roll_number': roll_number,
            'department': department,
            'class_name': class_name,
            'school': school,
            'phone_number': phone_number,
            'email': email,
            'teacher_id': teacher_id,
            'created_at': firestore.SERVER_TIMESTAMP
        }
        
        db.collection('students').add(new_student)
        
        flash('Student added successfully!')
        return redirect(url_for('students'))
    
    return render_template('add_student.html')

@app.route('/edit_student/<student_id>', methods=['GET', 'POST'])
@login_required
def edit_student(student_id):
    teacher_id = session.get('teacher_id')
    
    # Get the student
    student_doc = db.collection('students').document(student_id).get()
    
    if not student_doc.exists:
        flash('Student not found!')
        return redirect(url_for('students'))
    
    student = {'id': student_doc.id, **student_doc.to_dict()}
    
    # Verify ownership
    if student.get('teacher_id') != teacher_id:
        flash('You do not have permission to edit this student!')
        return redirect(url_for('students'))
    
    if request.method == 'POST':
        name = request.form.get('name')
        roll_number = request.form.get('roll_number')
        department = request.form.get('department')
        class_name = request.form.get('class_name')
        school = request.form.get('school')
        phone_number = request.form.get('phone_number')
        email = request.form.get('email')
        
        # Update student data
        updated_data = {
            'name': name,
            'roll_number': roll_number,
            'department': department,
            'class_name': class_name,
            'school': school,
            'phone_number': phone_number,
            'email': email,
            'updated_at': firestore.SERVER_TIMESTAMP
        }
        
        db.collection('students').document(student_id).update(updated_data)
        
        flash('Student updated successfully!')
        return redirect(url_for('students'))
    
    return render_template('edit_student.html', student=student)

@app.route('/delete_student/<student_id>')
@login_required
def delete_student(student_id):
    teacher_id = session.get('teacher_id')
    
    # Get the student
    student_doc = db.collection('students').document(student_id).get()
    
    if not student_doc.exists:
        flash('Student not found!')
        return redirect(url_for('students'))
    
    student = student_doc.to_dict()
    
    # Verify ownership
    if student.get('teacher_id') != teacher_id:
        flash('You do not have permission to delete this student!')
        return redirect(url_for('students'))
    
    # Delete associated submissions first
    submissions_ref = db.collection('submissions').where('student_id', '==', student_id).stream()
    for submission in submissions_ref:
        submission.reference.delete()
    
    # Delete the student
    db.collection('students').document(student_id).delete()
    
    flash('Student deleted successfully!')
    return redirect(url_for('students'))

@app.route('/assignments')
@login_required
def assignments():
    teacher_id = session.get('teacher_id')
    
    # Get all assignments for this teacher
    assignments_ref = db.collection('assignments').where('teacher_id', '==', teacher_id)
    assignments_docs = assignments_ref.order_by('created_at', direction=firestore.Query.DESCENDING).get()
    
    assignments_data = []
    for doc in assignments_docs:
        assignment = {'id': doc.id, **doc.to_dict()}
        
        # Convert timestamps using our utility function
        if 'due_date' in assignment:
            assignment['due_date'] = convert_firebase_timestamp(assignment['due_date'])
        
        if 'created_at' in assignment:
            assignment['created_at'] = convert_firebase_timestamp(assignment['created_at'])
        
        # Calculate completion counts
        assignment_id = doc.id
        submissions_ref = db.collection('submissions').where('assignment_id', '==', assignment_id).get()
        total_count = len(list(submissions_ref))
        completed_count = 0
        
        for submission_doc in submissions_ref:
            submission_data = submission_doc.to_dict()
            if submission_data.get('status') == 'completed':
                completed_count += 1
        
        assignment['completed_count'] = completed_count
        assignment['total_count'] = total_count
        assignment['completion_rate'] = (completed_count / total_count * 100) if total_count > 0 else 0
            
        assignments_data.append(assignment)
    
    # Add the current datetime for comparing with due dates
    return render_template('assignments.html', assignments=assignments_data, now=datetime.now())

@app.route('/add_assignment', methods=['GET', 'POST'])
@login_required
def add_assignment():
    teacher_id = session.get('teacher_id')
    
    # Get all students for this teacher to populate the specific students section
    students_ref = db.collection('students').where('teacher_id', '==', teacher_id)
    all_students = [{'id': doc.id, **doc.to_dict()} for doc in students_ref.get()]
    
    # Sort students by roll number
    all_students = sort_students_by_roll(all_students)
    
    # Get class and department lists for dropdowns
    class_list = sorted(list(set(student.get('class_name', '') for student in all_students if student.get('class_name'))))
    department_list = sorted(list(set(student.get('department', '') for student in all_students if student.get('department'))))
    school_list = sorted(list(set(student.get('school', '') for student in all_students if student.get('school'))))
    
    if request.method == 'POST':
        title = request.form.get('title')
        description = request.form.get('description')
        due_date_str = request.form.get('due_date')
        points = request.form.get('points', 100)
        
        # Convert string datetime to Firestore timestamp
        if due_date_str:
            due_date = datetime.fromisoformat(due_date_str)
            due_date_timestamp = due_date.timestamp()
        else:
            due_date = None
            due_date_timestamp = None
        
        # Handle assignment targeting options
        target_type = request.form.get('target_type', 'all')
        target_value = None
        
        if target_type == 'class':
            target_value = request.form.get('class_target')
        elif target_type == 'department':
            target_value = request.form.get('department_target')
        elif target_type == 'school':
            target_value = request.form.get('school_target')
        elif target_type == 'specific':
            target_value = request.form.getlist('student_targets')
        
        # Create new assignment
        new_assignment = {
            'title': title,
            'description': description,
            'due_date': due_date_timestamp,
            'points': int(points) if points else 100,
            'teacher_id': teacher_id,
            'target_type': target_type,
            'target_value': target_value,
            'visible': True,
            'created_at': firestore.SERVER_TIMESTAMP
        }
        
        # Fixed: Add the document first and then get the document reference
        doc_ref = db.collection('assignments').document()  # Create a new document reference
        doc_ref.set(new_assignment)  # Set the data to this document
        assignment_id = doc_ref.id  # Get the document ID
        
        # For specific student targeting, create submission placeholders
        if target_type == 'specific' and target_value:
            for student_id in target_value:
                submission = {
                    'assignment_id': assignment_id,  # Use the saved ID
                    'student_id': student_id,
                    'teacher_id': teacher_id,
                    'status': 'pending',
                    'score': None,
                    'feedback': None,
                    'created_at': firestore.SERVER_TIMESTAMP
                }
                db.collection('submissions').add(submission)
        
        flash('Assignment created successfully!')
        return redirect(url_for('assignments'))
    
    return render_template('add_assignment.html', 
                          class_list=class_list,
                          department_list=department_list,
                          school_list=school_list,
                          all_students=all_students)  # Pass all_students to the template

@app.route('/edit_assignment/<assignment_id>', methods=['GET', 'POST'])
@login_required
def edit_assignment(assignment_id):
    teacher_id = session.get('teacher_id')
    
    # Get the assignment
    assignment_doc = db.collection('assignments').document(assignment_id).get()
    
    if not assignment_doc.exists:
        flash('Assignment not found!')
        return redirect(url_for('assignments'))
    
    assignment = {'id': assignment_doc.id, **assignment_doc.to_dict()}
    
    # Verify ownership
    if assignment.get('teacher_id') != teacher_id:
        flash('You do not have permission to edit this assignment!')
        return redirect(url_for('assignments'))
    
    # Get all students for this teacher to populate the specific students section
    students_ref = db.collection('students').where('teacher_id', '==', teacher_id)
    all_students = [{'id': doc.id, **doc.to_dict()} for doc in students_ref.get()]
    
    # Get class and department lists for dropdowns
    class_list = sorted(list(set(student.get('class_name', '') for student in all_students if student.get('class_name'))))
    department_list = sorted(list(set(student.get('department', '') for student in all_students if student.get('department'))))
    school_list = sorted(list(set(student.get('school', '') for student in all_students if student.get('school'))))
    
    # Convert timestamp to datetime for the template
    if 'due_date' in assignment and assignment['due_date']:
        # Convert the timestamp stored as a float back to a datetime
        assignment['due_date'] = convert_firebase_timestamp(assignment['due_date'])
    
    if request.method == 'POST':
        title = request.form.get('title')
        description = request.form.get('description')
        due_date_str = request.form.get('due_date')
        points = request.form.get('points', 100)
        visible = 'visible' in request.form
        
        # Handle assignment targeting options
        target_type = request.form.get('target_type', 'all')
        target_value = None
        
        if target_type == 'class':
            target_value = request.form.get('class_target')
        elif target_type == 'department':
            target_value = request.form.get('department_target')
        elif target_type == 'school':
            target_value = request.form.get('school_target')
        elif target_type == 'specific':
            target_value = request.form.getlist('student_targets')
        
        # Convert string datetime to timestamp
        if due_date_str:
            due_date = datetime.fromisoformat(due_date_str)
            due_date_timestamp = due_date.timestamp()
        else:
            due_date_timestamp = None
        
        # Update assignment
        updated_data = {
            'title': title,
            'description': description,
            'due_date': due_date_timestamp,
            'points': int(points),
            'target_type': target_type,
            'target_value': target_value,
            'visible': visible,
            'updated_at': firestore.SERVER_TIMESTAMP
        }
        
        db.collection('assignments').document(assignment_id).update(updated_data)
        
        # If assignment is changed to specific students, update submissions
        if target_type == 'specific' and target_value:
            # Get current submissions
            current_submissions = db.collection('submissions').where('assignment_id', '==', assignment_id).stream()
            
            # Track which students already have submissions
            existing_student_ids = set()
            for submission in current_submissions:
                submission_data = submission.to_dict()
                existing_student_ids.add(submission_data.get('student_id'))
            
            # Create submissions for newly added students
            for student_id in target_value:
                if student_id not in existing_student_ids:
                    new_submission = {
                        'assignment_id': assignment_id,
                        'student_id': student_id,
                        'teacher_id': teacher_id,
                        'status': 'pending',
                        'score': None,
                        'feedback': None,
                        'created_at': firestore.SERVER_TIMESTAMP
                    }
                    db.collection('submissions').add(new_submission)
        
        flash('Assignment updated successfully!')
        return redirect(url_for('assignments'))
    
    return render_template('edit_assignment.html', 
                          assignment=assignment,
                          class_list=class_list,
                          department_list=department_list,
                          school_list=school_list,
                          all_students=all_students)

@app.route('/delete_assignment/<assignment_id>')
@login_required
def delete_assignment(assignment_id):
    teacher_id = session.get('teacher_id')
    
    # Get the assignment
    assignment_doc = db.collection('assignments').document(assignment_id).get()
    
    if not assignment_doc.exists:
        flash('Assignment not found!')
        return redirect(url_for('assignments'))
    
    assignment = assignment_doc.to_dict()
    
    # Verify ownership
    if assignment.get('teacher_id') != teacher_id:
        flash('You do not have permission to delete this assignment!')
        return redirect(url_for('assignments'))
    
    # Delete associated submissions first (Firebase doesn't have cascading deletes)
    submissions_ref = db.collection('submissions').where('assignment_id', '==', assignment_id).stream()
    for submission in submissions_ref:
        submission.reference.delete()
    
    # Delete the assignment
    db.collection('assignments').document(assignment_id).delete()
    
    flash('Assignment deleted successfully!')
    return redirect(url_for('assignments'))

@app.route('/student_progress', methods=['GET', 'POST'])
@login_required
def student_progress():
    teacher_id = session.get('teacher_id')
    
    # Get all students for this teacher and sort by roll number
    students_ref = db.collection('students').where('teacher_id', '==', teacher_id)
    students_data = [{'id': doc.id, **doc.to_dict()} for doc in students_ref.get()]
    
    # Sort students by roll number
    students_data = sort_students_by_roll(students_data)
    
    selected_student = None
    assignments_data = []
    completion_rate = 0
    
    # Unified parameter retrieval from both GET and POST
    student_id = request.args.get('student_id') or request.form.get('student_id')
    
    if student_id:
        try:
            # Get the selected student with error handling
            student_doc = db.collection('students').document(student_id).get()
            
            if student_doc.exists:
                selected_student = {'id': student_doc.id, **student_doc.to_dict()}
                
                # Get all submissions for this student first to avoid multiple queries
                submissions_ref = db.collection('submissions').where('student_id', '==', student_id).get()
                
                # Create a lookup dictionary for quick access to submission data
                submissions_lookup = {}
                for sub_doc in submissions_ref:
                    sub_data = {'id': sub_doc.id, **sub_doc.to_dict()}
                    assignment_id = sub_data.get('assignment_id')
                    
                    # Convert timestamps
                    if 'submission_date' in sub_data and sub_data['submission_date']:
                        sub_data['submission_date'] = convert_firebase_timestamp(sub_data['submission_date'])
                    
                    if 'updated_at' in sub_data and sub_data['updated_at']:
                        sub_data['updated_at'] = convert_firebase_timestamp(sub_data['updated_at'])
                    
                    if 'created_at' in sub_data and sub_data['created_at']:
                        sub_data['created_at'] = convert_firebase_timestamp(sub_data['created_at'])
                    
                    # Store in lookup with assignment_id as key
                    submissions_lookup[assignment_id] = sub_data
                
                # Get all assignments for this teacher
                assignments_ref = db.collection('assignments').where('teacher_id', '==', teacher_id).get()
                
                for assignment_doc in assignments_ref:
                    try:
                        assignment = {'id': assignment_doc.id, **assignment_doc.to_dict()}
                        
                        # Ensure due_date is properly converted
                        if 'due_date' in assignment and assignment['due_date']:
                            try:
                                # Handle numeric timestamp
                                if isinstance(assignment['due_date'], (int, float)):
                                    assignment['due_date'] = datetime.fromtimestamp(assignment['due_date'])
                                # Handle Firestore timestamp
                                elif hasattr(assignment['due_date'], 'datetime'):
                                    assignment['due_date'] = assignment['due_date'].datetime()
                            except Exception as e:
                                print(f"Error converting due_date for assignment {assignment_doc.id}: {e}")
                                # If conversion fails, use current time as fallback
                                assignment['due_date'] = datetime.now()
                        
                        # Check if assignment applies to this student
                        target_type = assignment.get('target_type', 'all')
                        target_value = assignment.get('target_value')
                        applies_to_student = False
                        
                        if target_type == 'all':
                            applies_to_student = True
                        elif target_type == 'class' and target_value == selected_student.get('class_name'):
                            applies_to_student = True
                        elif target_type == 'department' and target_value == selected_student.get('department'):
                            applies_to_student = True
                        elif target_type == 'school' and target_value == selected_student.get('school'):
                            applies_to_student = True
                        elif target_type == 'specific' and isinstance(target_value, list) and student_id in target_value:
                            applies_to_student = True
                        
                        if applies_to_student:
                            # First create a base assignment object with default values
                            assignment_details = {
                                'id': assignment['id'],
                                'title': assignment['title'],
                                'description': assignment.get('description', ''),
                                'due_date': assignment.get('due_date'),
                                'points': assignment.get('points', 100),
                                'status': 'pending',
                                'score': None,
                                'feedback': None,
                                'submission_date': None,
                                'is_completed': False,
                                'is_overdue': False
                            }
                            
                            # Check if we have a submission in our lookup
                            submission = submissions_lookup.get(assignment_doc.id)
                            
                            if submission:
                                # Set the submission date value
                                submission_date = submission.get('submission_date') or submission.get('updated_at')
                                
                                # Update assignment details with submission data
                                assignment_details.update({
                                    'status': submission.get('status', 'pending'),
                                    'score': submission.get('score'),
                                    'feedback': submission.get('feedback'),
                                    'submission_date': submission_date,
                                    # Critical fix: properly check the status exactly
                                    'is_completed': submission.get('status') == 'completed'
                                })
                            else:
                                # Create a default submission if none exists
                                new_submission = {
                                    'assignment_id': assignment_doc.id,
                                    'student_id': student_id,
                                    'teacher_id': teacher_id,
                                    'status': 'pending',
                                    'score': None,
                                    'feedback': None,
                                    'created_at': firestore.SERVER_TIMESTAMP
                                }
                                
                                # Add to Firestore
                                db.collection('submissions').add(new_submission)
                            
                            # Add overdue flag with proper error handling
                            if assignment_details.get('due_date') and not assignment_details.get('is_completed', False):
                                try:
                                    assignment_details['is_overdue'] = assignment_details['due_date'] < datetime.now()
                                except Exception as e:
                                    print(f"Error setting overdue flag: {e}")
                                    assignment_details['is_overdue'] = False
                            
                            assignments_data.append(assignment_details)
                    except Exception as e:
                        print(f"Error processing assignment {assignment_doc.id}: {e}")
                
                # Sort assignments by due date (newest first)
                try:
                    assignments_data.sort(key=lambda x: x.get('due_date') or datetime.max, reverse=True)
                except Exception as e:
                    print(f"Error sorting assignments: {e}")
                
                # Calculate completion rate
                if assignments_data:
                    completed = sum(1 for a in assignments_data if a.get('is_completed', False))
                    completion_rate = round((completed / len(assignments_data)) * 100) if len(assignments_data) > 0 else 0
        except Exception as e:
            import traceback
            print(f"Error in student progress processing: {e}")
            print(traceback.format_exc())
            flash(f"An error occurred: {str(e)}", "danger")
    
    # Ensure all data is safe for the template
    context = {
        'students': students_data,
        'selected_student': selected_student,
        'assignments_data': assignments_data,
        'completion_rate': completion_rate,
        'now': datetime.now()
    }
    
    return render_template('student_progress.html', **context)

@app.route('/assignment_status/<assignment_id>')
@login_required
def assignment_status(assignment_id):
    teacher_id = session.get('teacher_id')
    
    # Get the assignment
    assignment_doc = db.collection('assignments').document(assignment_id).get()
    
    if not assignment_doc.exists:
        flash('Assignment not found!')
        return redirect(url_for('assignments'))
    
    assignment = {'id': assignment_doc.id, **assignment_doc.to_dict()}
    
    # Verify ownership
    if assignment.get('teacher_id') != teacher_id:
        flash('You do not have permission to view this assignment!')
        return redirect(url_for('assignments'))
    
    # Fixed: Convert timestamps properly
    if 'due_date' in assignment and assignment['due_date']:
        try:
            # If due_date is stored as a timestamp (float)
            assignment['due_date'] = datetime.fromtimestamp(assignment['due_date'])
        except (TypeError, ValueError):
            # If it's already a datetime or a different format, keep it as is
            pass
    
    # Get all submissions for this assignment
    submissions_ref = db.collection('submissions').where('assignment_id', '==', assignment_id).get()
    submissions_data = []
    
    # Create a dictionary to track student status
    status_dict = {}
    
    for submission_doc in submissions_ref:
        submission = {'id': submission_doc.id, **submission_doc.to_dict()}
        
        # Get student info
        student_doc = db.collection('students').document(submission['student_id']).get()
        # Fixed: Changed from student_doc.exists() to student_doc.exists
        if student_doc.exists:
            student = {'id': student_doc.id, **student_doc.to_dict()}
            submission['student_name'] = student.get('name')
            submission['roll_number'] = student.get('roll_number')
            submission['class_name'] = student.get('class_name')
            
            # Convert any timestamps in submission
            if 'submission_date' in submission and submission['submission_date']:
                try:
                    submission['submission_date'] = datetime.fromtimestamp(submission['submission_date'])
                except (TypeError, ValueError, AttributeError):
                    try:
                        submission['submission_date'] = submission['submission_date'].datetime()
                    except AttributeError:
                        pass
            
            # Add to status dictionary
            status_dict[student['id']] = {
                'status': submission.get('status', 'pending'),
                'score': submission.get('score'),
                'feedback': submission.get('feedback'),
                'completed': submission.get('status') == 'completed',
                'submission_date': submission.get('submission_date') or submission.get('updated_at'),
                'id': submission['id']  # The submission ID is included here
            }
            
            submissions_data.append(submission)
    
    # Get all students assigned to this assignment
    students = []
    
    # Different handling based on target_type
    target_type = assignment.get('target_type')
    target_value = assignment.get('target_value')
    
    if target_type == 'all':
        students_ref = db.collection('students').where('teacher_id', '==', teacher_id).get()
        students = [{'id': doc.id, **doc.to_dict()} for doc in students_ref]
    elif target_type == 'class' and target_value:
        students_ref = db.collection('students').where('teacher_id', '==', teacher_id).where('class_name', '==', target_value).get()
        students = [{'id': doc.id, **doc.to_dict()} for doc in students_ref]
    elif target_type == 'department' and target_value:
        students_ref = db.collection('students').where('teacher_id', '==', teacher_id).where('department', '==', target_value).get()
        students = [{'id': doc.id, **doc.to_dict()} for doc in students_ref]
    elif target_type == 'school' and target_value:
        students_ref = db.collection('students').where('teacher_id', '==', teacher_id).where('school', '==', target_value).get()
        students = [{'id': doc.id, **doc.to_dict()} for doc in students_ref]
    elif target_type == 'specific' and target_value:
        students = []
        for student_id in target_value:
            student_doc = db.collection('students').document(student_id).get()
            if student_doc.exists:
                students.append({'id': student_doc.id, **student_doc.to_dict()})
    
    # After getting the students list, sort them by roll number
    students = sort_students_by_roll(students)
    
    # Make sure all students have a status entry
    for student in students:
        if student['id'] not in status_dict:
            # First, check if we need to create a submission for this student
            need_new_submission = True
            
            # Create a new submission entry in the database for this student
            if need_new_submission:
                new_submission = {
                    'assignment_id': assignment_id,
                    'student_id': student['id'],
                    'teacher_id': teacher_id,
                    'status': 'pending',
                    'score': None,
                    'feedback': None,
                    'created_at': firestore.SERVER_TIMESTAMP
                }
                
                # Add the submission to Firestore and get the new ID
                submission_ref = db.collection('submissions').add(new_submission)
                submission_id = submission_ref[1].id
                
                # Now use this ID in the status_dict
                status_dict[student['id']] = {
                    'status': 'pending',
                    'score': None,
                    'feedback': None,
                    'completed': False,
                    'submission_date': None,
                    'id': submission_id  # Include the new submission ID
                }
    
    # Generate a human-readable targeting info string
    target_info = None
    if target_type == 'all':
        target_info = "All Students"
    elif target_type == 'class':
        target_info = f"Class: {target_value}"
    elif target_type == 'department':
        target_info = f"Board: {target_value}"
    elif target_type == 'school':
        target_info = f"School: {target_value}"
    elif target_type == 'specific':
        target_info = f"{len(target_value)} Specific Students"
    
    # Before returning the template, ensure we have accurate completion counts
    completed_count = sum(1 for status in status_dict.values() if status.get('completed', False))
    total_count = len(students)
    completion_percentage = int((completed_count / total_count * 100) if total_count > 0 else 0)
    
    # Enhanced debug logging
    print(f"Status calculation: {completed_count} completed out of {total_count} students")
    print(f"Completion percentage: {completion_percentage}%")
    print(f"Status dictionary has {len(status_dict)} entries")
    
    return render_template('assignment_status.html', 
                          assignment=assignment,
                          submissions=submissions_data,
                          students=students,
                          status_dict=status_dict,
                          target_info=target_info,
                          completed_count=completed_count,  # Pass the count explicitly
                          completion_percentage=completion_percentage,  # Pass the percentage explicitly
                          now=datetime.now())

@app.route('/update_submission/<submission_id>', methods=['POST'])
@login_required
def update_submission(submission_id):
    teacher_id = session.get('teacher_id')
    
    # Get the submission
    submission_doc = db.collection('submissions').document(submission_id).get()
    
    if not submission_doc.exists:
        flash('Submission not found!')
        return redirect(url_for('assignments'))
    
    submission = submission_doc.to_dict()
    
    # Verify ownership
    if submission.get('teacher_id') != teacher_id:
        flash('You do not have permission to update this submission!')
        return redirect(url_for('assignments'))
    
    status = request.form.get('status')
    score = request.form.get('score')
    feedback = request.form.get('feedback')
    completed = 'completed' in request.form
    
    # Update submission
    updated_data = {
        'status': 'completed' if completed else 'pending',
        'score': int(score) if score and score.isdigit() else None,
        'feedback': feedback,
        'updated_at': firestore.SERVER_TIMESTAMP
    }
    
    db.collection('submissions').document(submission_id).update(updated_data)
    
    flash('Submission updated successfully!')
    return redirect(url_for('assignment_status', assignment_id=submission.get('assignment_id')))

@app.route('/update_status/<status_id>', methods=['POST'])
@login_required
def update_status(status_id):
    teacher_id = session.get('teacher_id')
    
    print(f"Update status called for submission ID: {status_id}")
    print(f"Form data: {request.form}")
    
    # Get the submission
    submission_doc = db.collection('submissions').document(status_id).get()
    
    if not submission_doc.exists:
        print(f"Submission not found with ID: {status_id}")
        flash('Status entry not found!')
        return redirect(url_for('assignments'))
    
    submission = submission_doc.to_dict()
    print(f"Found submission: {submission}")
    
    # Verify ownership
    if submission.get('teacher_id') != teacher_id:
        flash('You do not have permission to update this status!')
        return redirect(url_for('assignments'))
    
    # Get the completed status from checkbox
    completed = 'completed' in request.form
    
    # Update submission with all important fields
    updated_data = {
        'status': 'completed' if completed else 'pending',
        'updated_at': firestore.SERVER_TIMESTAMP
    }
    
    # Set submission_date only when completing for the first time
    if completed and not submission.get('submission_date'):
        updated_data['submission_date'] = firestore.SERVER_TIMESTAMP
    
    # Perform the update and handle any errors
    try:
        db.collection('submissions').document(status_id).update(updated_data)
        
        # Force a delay to ensure Firebase has time to update
        import time
        time.sleep(0.5)
        
        print(f"Successfully updated submission status to: {'completed' if completed else 'pending'}")
        flash('Status updated successfully!', 'success')
    except Exception as e:
        print(f"Error updating status: {str(e)}")
        flash(f'Error updating status: {str(e)}', 'danger')
    
    # Get the assignment ID to redirect back to assignment status page
    assignment_id = submission.get('assignment_id')
    return redirect(url_for('assignment_status', assignment_id=assignment_id))

@app.route('/logout')
def logout():
    # Clear session
    session.clear()
    return redirect(url_for('login'))

@app.route('/export_all_students')
@login_required
def export_all_students():
    teacher_id = session.get('teacher_id')
    
    try:
        # Get all students for this teacher
        students_ref = db.collection('students').where('teacher_id', '==', teacher_id).get()
        students = []
        for doc in students_ref:
            student_data = {'id': doc.id, **doc.to_dict()}
            # Convert any datetime fields to timezone-naive format
            for key, value in student_data.items():
                if isinstance(value, datetime) or hasattr(value, 'datetime'):
                    student_data[key] = convert_firebase_timestamp(value)
            students.append(student_data)
        
        # Get all assignments for this teacher
        assignments_ref = db.collection('assignments').where('teacher_id', '==', teacher_id).get()
        assignments = []
        for doc in assignments_ref:
            assignment_data = {'id': doc.id, **doc.to_dict()}
            # Convert timestamps
            if 'due_date' in assignment_data:
                assignment_data['due_date'] = convert_firebase_timestamp(assignment_data['due_date'])
            if 'created_at' in assignment_data:
                assignment_data['created_at'] = convert_firebase_timestamp(assignment_data['created_at'])
            assignments.append(assignment_data)
        
        # Get all submissions for lookup
        all_submissions = {}
        submissions_ref = db.collection('submissions').where('teacher_id', '==', teacher_id).get()
        for doc in submissions_ref:
            sub_data = {'id': doc.id, **doc.to_dict()}
            student_id = sub_data.get('student_id')
            assignment_id = sub_data.get('assignment_id')
            
            if student_id not in all_submissions:
                all_submissions[student_id] = {}
            
            all_submissions[student_id][assignment_id] = {
                'status': sub_data.get('status'),
                'score': sub_data.get('score'),
                'feedback': sub_data.get('feedback'),
                'submission_date': convert_firebase_timestamp(sub_data.get('updated_at')),
                'completed': sub_data.get('status') == 'completed'
            }
        
        # Process students data for Excel - simplified for direct export
        student_data_for_excel = []
        for student in students:
            student_data_for_excel.append({
                'Name': student.get('name', ''),
                'Roll Number': student.get('roll_number', ''),
                'Department': student.get('department', ''),
                'Class': student.get('class_name', ''),
                'School': student.get('school', ''),
                'Phone Number': student.get('phone_number', ''),
                'Email': student.get('email', '')
            })
        
        # Create detailed assignment data - simplified without target type
        detailed_data = []
        for student in students:
            student_id = student['id']
            
            for assignment in assignments:
                assignment_id = assignment['id']
                
                # Check if student has a submission for this assignment
                has_submission = student_id in all_submissions and assignment_id in all_submissions[student_id]
                
                if has_submission:
                    submission_info = all_submissions[student_id][assignment_id]
                    row = {
                        'Student Name': student.get('name', ''),
                        'Roll Number': student.get('roll_number', ''),
                        'Class': student.get('class_name', ''),
                        'Department': student.get('department', ''),
                        'School': student.get('school', ''),
                        'Assignment': assignment.get('title', ''),
                        'Due Date': assignment.get('due_date').strftime('%Y-%m-%d %H:%M') if assignment.get('due_date') else '',
                        'Status': 'Yes' if submission_info.get('completed', False) else 'No',
                        'Submission Date': submission_info.get('submission_date').strftime('%Y-%m-%d %H:%M') if submission_info.get('submission_date') else 'Not submitted',
                        'Score': submission_info.get('score', ''),
                        'Feedback': submission_info.get('feedback', '')
                    }
                    detailed_data.append(row)
        
        # Create Excel with multiple sheets (simplified)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            workbook = writer.book
            
            # Create formats
            header_format = workbook.add_format({
                'bold': True,
                'bg_color': '#4361ee',
                'font_color': 'white',
                'border': 1,
                'align': 'center',
                'valign': 'vcenter'
            })
            
            cell_format = workbook.add_format({
                'border': 1,
                'align': 'left',
                'valign': 'vcenter'
            })
            
            completed_format = workbook.add_format({
                'bg_color': '#d1e7dd',
                'border': 1,
                'align': 'center',
                'valign': 'vcenter'
            })
            
            incomplete_format = workbook.add_format({
                'bg_color': '#f8d7da',
                'border': 1,
                'align': 'center',
                'valign': 'vcenter'
            })
            
            title_format = workbook.add_format({
                'bold': True,
                'font_size': 14,
                'align': 'center',
                'valign': 'vcenter',
                'bg_color': '#E8EAED',
                'border': 0
            })
            
            # Students information sheet
            if student_data_for_excel:
                df_students = pd.DataFrame(student_data_for_excel)
                df_students.to_excel(writer, sheet_name='All Students', index=False, startrow=1)
                worksheet = writer.sheets['All Students']
                
                # Add title
                worksheet.merge_range('A1:G1', "All Students Information", title_format)
                worksheet.set_row(0, 30)
                
                # Format headers
                for col_num, value in enumerate(df_students.columns.values):
                    worksheet.write(1, col_num, value, header_format)
                    worksheet.set_column(col_num, col_num, 18)
                
                # Format data cells
                for row in range(len(df_students)):
                    for col in range(len(df_students.columns)):
                        cell_value = df_students.iloc[row, col]
                        if pd.isna(cell_value) or (isinstance(cell_value, float) and (cell_value == float('inf') or cell_value == float('-inf'))):
                            worksheet.write(row + 2, col, '', cell_format)
                        else:
                            worksheet.write(row + 2, col, cell_value, cell_format)
            
            # Detailed assignment information - simplified
            if detailed_data:
                df_detailed = pd.DataFrame(detailed_data)
                df_detailed.to_excel(writer, sheet_name='Assignment Details', index=False, startrow=1)
                worksheet = writer.sheets['Assignment Details']
                
                # Add title
                title_cell = 'A1:L1'  # Adjust based on number of columns
                worksheet.merge_range(title_cell, "Student Assignment Status", title_format)
                worksheet.set_row(0, 30)
                
                # Format headers
                for col_num, value in enumerate(df_detailed.columns.values):
                    worksheet.write(1, col_num, value, header_format)
                    worksheet.set_column(col_num, col_num, 15)
                
                # Set special column widths
                worksheet.set_column(df_detailed.columns.get_loc('Assignment') if 'Assignment' in df_detailed.columns else 5, 
                                     df_detailed.columns.get_loc('Assignment') if 'Assignment' in df_detailed.columns else 5, 25)
                worksheet.set_column(df_detailed.columns.get_loc('Feedback') if 'Feedback' in df_detailed.columns else 10, 
                                     df_detailed.columns.get_loc('Feedback') if 'Feedback' in df_detailed.columns else 10, 30)
                
                # Format data with conditional formatting
                for row in range(len(df_detailed)):
                    for col in range(len(df_detailed.columns)):
                        cell_value = df_detailed.iloc[row, col]
                        
                        # Special formatting for Status column
                        if df_detailed.columns[col] == 'Status':
                            format_to_use = completed_format if cell_value == 'Yes' else incomplete_format
                        else:
                            format_to_use = cell_format
                        
                        if pd.isna(cell_value) or (isinstance(cell_value, float) and (cell_value == float('inf') or cell_value == float('-inf'))):
                            worksheet.write(row + 2, col, '', format_to_use)
                        else:
                            worksheet.write(row + 2, col, cell_value, format_to_use)
        
        output.seek(0)
        
        # Return the Excel file
        return send_file(
            output, 
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'all_students_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
        )
    
    except Exception as e:
        import traceback
        print(f"Error exporting all students: {str(e)}")
        print(traceback.format_exc())
        flash(f'Error exporting data: {str(e)}')
        return redirect(url_for('students'))

@app.route('/export_student/<student_id>')
@login_required
def export_student(student_id):
    teacher_id = session.get('teacher_id')
    
    # Get the specific student
    student_doc = db.collection('students').document(student_id).get()
    
    if not student_doc.exists:
        flash('Student not found!')
        return redirect(url_for('students'))
    
    student = student_doc.to_dict()
    student_id_value = student_doc.id
    
    # Verify ownership
    if student.get('teacher_id') != teacher_id:
        flash('You do not have permission to export this student!')
        return redirect(url_for('students'))
    
    try:
        # Get assignment submissions for this student
        submissions_query = db.collection('submissions').where('student_id', '==', student_id).get()
        submission_dict = {}
        for sub_doc in submissions_query:
            sub_data = sub_doc.to_dict()
            submission_dict[sub_data.get('assignment_id')] = {
                'status': sub_data.get('status'),
                'score': sub_data.get('score'),
                'feedback': sub_data.get('feedback'),
                'submission_date': convert_firebase_timestamp(sub_data.get('updated_at')),
                'completed': sub_data.get('status') == 'completed'
            }
        
        # Get assignments for this student - simplified to show all assignments with submissions
        assignments_data = []
        assignments_ref = db.collection('assignments').where('teacher_id', '==', teacher_id).get()
        
        for assignment_doc in assignments_ref:
            assignment = {'id': assignment_doc.id, **assignment_doc.to_dict()}
            assignment_id = assignment_doc.id
            
            # Convert timestamps
            if 'due_date' in assignment:
                assignment['due_date'] = convert_firebase_timestamp(assignment['due_date'])
            
            # Only include if there's a submission
            if assignment_id in submission_dict:
                submission_info = submission_dict[assignment_id]
                
                # Prepare assignment data
                assignment_data = {
                    'Assignment': assignment.get('title', ''),
                    'Description': assignment.get('description', ''),
                    'Due Date': assignment.get('due_date').strftime('%Y-%m-%d %H:%M') if assignment.get('due_date') else '',
                    'Completed': 'Yes' if submission_info.get('completed', False) else 'No',
                    'Submission Date': submission_info.get('submission_date').strftime('%Y-%m-%d %H:%M') if submission_info.get('submission_date') else 'Not submitted',
                    'Score': submission_info.get('score', ''),
                    'Feedback': submission_info.get('feedback', '')
                }
                
                assignments_data.append(assignment_data)
        
        # Student basic information
        student_info = {
            'Name': student.get('name', ''),
            'Roll Number': student.get('roll_number', ''),
            'Department': student.get('department', ''),
            'Class': student.get('class_name', ''),
            'School': student.get('school', ''),
            'Phone Number': student.get('phone_number', ''),
            'Email': student.get('email', '')
        }
        
        # Prepare completion summary - simplified
        completion_data = {
            'Student Name': student.get('name', ''),
            'Roll Number': student.get('roll_number', '')
        }
        
        for assignment_data in assignments_data:
            assignment_title = assignment_data['Assignment']
            completion_data[assignment_title] = assignment_data['Completed']
        
        # Create Excel file with advanced formatting
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            workbook = writer.book
            
            # Create formats for Excel styling
            header_format = workbook.add_format({
                'bold': True,
                'bg_color': '#4361ee',
                'font_color': 'white',
                'border': 1,
                'align': 'center',
                'valign': 'vcenter'
            })
            
            cell_format = workbook.add_format({
                'border': 1,
                'align': 'left',
                'valign': 'vcenter'
            })
            
            completed_format = workbook.add_format({
                'bg_color': '#d1e7dd',
                'border': 1,
                'align': 'center',
                'valign': 'vcenter'
            })
            
            incomplete_format = workbook.add_format({
                'bg_color': '#f8d7da',
                'border': 1,
                'align': 'center',
                'valign': 'vcenter'
            })
            
            title_format = workbook.add_format({
                'bold': True,
                'font_size': 14,
                'align': 'center',
                'valign': 'vcenter',
                'bg_color': '#E8EAED',
                'border': 0
            })
            
            # Write student info as DataFrame
            df_student = pd.DataFrame([student_info])
            df_student.to_excel(writer, sheet_name='Student Info', index=False)
            worksheet = writer.sheets['Student Info']
            
            # Add a title before the student info table
            worksheet.merge_range('A1:G1', f"Student Information - {student_info['Name']}", title_format)
            worksheet.set_row(0, 30)  # Set row height for title
            
            # Format headers and data (accounting for title row)
            for col_num, value in enumerate(df_student.columns.values):
                worksheet.write(1, col_num, value, header_format)
                worksheet.set_column(col_num, col_num, 20)  # Set column width
            
            # Write data row
            for col in range(len(df_student.columns)):
                cell_value = df_student.iloc[0, col]
                if pd.isna(cell_value) or (isinstance(cell_value, float) and (cell_value == float('inf') or cell_value == float('-inf'))):
                    worksheet.write(2, col, '', cell_format)
                else:
                    worksheet.write(2, col, cell_value, cell_format)
            
            # Assignment details sheet - just display all assignments directly
            if assignments_data:
                df_assignments = pd.DataFrame(assignments_data)
                df_assignments.to_excel(writer, sheet_name='Assignment Details', index=False)
                worksheet = writer.sheets['Assignment Details']
                
                # Add title
                worksheet.merge_range('A1:H1', f"Assignment Information - {student_info['Name']}", title_format)
                worksheet.set_row(0, 30)
                
                # Format headers
                for col_num, value in enumerate(df_assignments.columns.values):
                    worksheet.write(1, col_num, value, header_format)
                    worksheet.set_column(col_num, col_num, 18)
                
                # Special width for description column
                worksheet.set_column(1, 1, 30)
                
                # Format data
                for row in range(len(df_assignments)):
                    is_completed = df_assignments.iloc[row]['Completed'] == 'Yes'
                    
                    for col in range(len(df_assignments.columns)):
                        cell_value = df_assignments.iloc[row, col]
                        
                        # Only use special formatting for Completed column
                        if col == list(df_assignments.columns).index('Completed'):
                            used_format = completed_format if is_completed else incomplete_format
                        else:
                            used_format = cell_format
                        
                        if pd.isna(cell_value) or (isinstance(cell_value, float) and (cell_value == float('inf') or cell_value == float('-inf'))):
                            worksheet.write(row + 2, col, '', used_format)
                        else:
                            worksheet.write(row + 2, col, cell_value, used_format)
        
        output.seek(0)
        
        # Get student name for the filename
        student_name = student.get('name', 'student').replace(' ', '_')
        
        # Return the Excel file
        return send_file(
            output, 
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'{student_name}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
        )
    
    except Exception as e:
        import traceback
        print(f"Error exporting student data: {str(e)}")
        print(traceback.format_exc())
        flash(f'Error exporting student data: {str(e)}')
        return redirect(url_for('students'))

if __name__ == '__main__':
    app.run(debug=True)