"""
Utility script to check the submission status directly in Firestore database.
This can be used to debug issues with student progress.
"""

import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime

def convert_firebase_timestamp(timestamp_value):
    """Convert any Firebase timestamp format to a Python datetime."""
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

def main():
    # Initialize Firebase (use existing credentials file)
    cred = credentials.Certificate("firebase.json")
    try:
        firebase_admin.initialize_app(cred)
    except ValueError:
        # App already initialized
        pass
    
    db = firestore.client()
    
    # Query for a specific student (modify these parameters as needed)
    student_id = input("Enter student ID to check (or leave blank to check all): ").strip()
    assignment_id = input("Enter assignment ID to check (or leave blank to check all): ").strip()
    
    # Build query
    query = db.collection('submissions')
    
    if student_id:
        query = query.where('student_id', '==', student_id)
    
    if assignment_id:
        query = query.where('assignment_id', '==', assignment_id)
    
    # Execute query
    submissions = query.get()
    
    # Print results
    print("\n===== SUBMISSION STATUS =====")
    print(f"Found {len(list(submissions))} submissions")
    
    for sub in submissions:
        sub_data = sub.to_dict()
        print("\n---------------------------")
        print(f"Submission ID: {sub.id}")
        print(f"Student ID: {sub_data.get('student_id')}")
        print(f"Assignment ID: {sub_data.get('assignment_id')}")
        print(f"Status: {sub_data.get('status')}")
        print(f"Completed?: {'Yes' if sub_data.get('status') == 'completed' else 'No'}")
        
        # Convert and print timestamps
        if 'submission_date' in sub_data:
            date = convert_firebase_timestamp(sub_data['submission_date'])
            print(f"Submission Date: {date}")
        
        if 'updated_at' in sub_data:
            date = convert_firebase_timestamp(sub_data['updated_at'])
            print(f"Updated At: {date}")
            
        # Get related student info
        try:
            student = db.collection('students').document(sub_data.get('student_id')).get()
            if student.exists:
                student_data = student.to_dict()
                print(f"Student Name: {student_data.get('name')}")
                print(f"Student Roll: {student_data.get('roll_number')}")
        except Exception as e:
            print(f"Error getting student info: {e}")
        
        # Get related assignment info
        try:
            assignment = db.collection('assignments').document(sub_data.get('assignment_id')).get()
            if assignment.exists:
                assignment_data = assignment.to_dict()
                print(f"Assignment Title: {assignment_data.get('title')}")
        except Exception as e:
            print(f"Error getting assignment info: {e}")
    
    print("\n===== END OF REPORT =====")

if __name__ == "__main__":
    main()
