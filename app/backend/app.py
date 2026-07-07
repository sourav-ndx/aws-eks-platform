import os
import pymysql
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

def get_db():
    return pymysql.connect(
        host=os.environ['DB_HOST'],
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        database=os.environ['DB_NAME'],
        cursorclass=pymysql.cursors.DictCursor
    )

@app.route('/health')
@app.route('/api/health')
def health():
    return jsonify({"status": "healthy", "service": "itp-backend"})

@app.route('/employees')
@app.route('/api/employees')
def employees():
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM employees")
            result = cursor.fetchall()
        conn.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

app.run(host='0.0.0.0', port=5000)
