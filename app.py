from flask import Flask, jsonify, render_template, request, session, redirect, url_for
import sqlite3

app = Flask(__name__)
app.secret_key = 'super_secret_fixed_key_diploma_2024'
ADMIN_PASSWORD = 'admin123'

def get_db():
    conn = sqlite3.connect('laws.db')
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/law/<int:id>')
def law(id):
    return render_template('law.html', law_id=id)

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin'] = True
            return redirect(url_for('admin'))
        return render_template('login.html', error='Неверный пароль')
    if not session.get('admin'):
        return render_template('login.html', error=None)
    return render_template('admin.html')

@app.route('/admin/logout')
def logout():
    session.pop('admin', None)
    return redirect('/')

@app.route('/api/laws')
def get_laws():
    conn = get_db()
    sort = request.args.get('sort', 'default')
    if sort == 'popular':
        laws = conn.execute('SELECT * FROM laws ORDER BY views DESC').fetchall()
    else:
        laws = conn.execute('SELECT * FROM laws').fetchall()
    conn.close()
    return jsonify([dict(row) for row in laws])

@app.route('/api/laws/<int:id>')
def get_law(id):
    conn = get_db()
    conn.execute('UPDATE laws SET views = views + 1 WHERE id = ?', (id,))
    conn.commit()
    law = conn.execute('SELECT * FROM laws WHERE id = ?', (id,)).fetchone()
    articles = conn.execute('SELECT * FROM articles WHERE law_id = ?', (id,)).fetchall()
    conn.close()
    if law is None:
        return jsonify({'error': 'Не найдено'}), 404
    result = dict(law)
    result['articles'] = [dict(a) for a in articles]
    return jsonify(result)

@app.route('/api/laws', methods=['POST'])
def add_law():
    data = request.json
    conn = get_db()
    conn.execute(
        'INSERT INTO laws (number, title, description, category, year) VALUES (?, ?, ?, ?, ?)',
        (data['number'], data['title'], data['description'], data['category'], data['year'])
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/laws/<int:id>', methods=['DELETE'])
def delete_law(id):
    conn = get_db()
    conn.execute('DELETE FROM articles WHERE law_id = ?', (id,))
    conn.execute('DELETE FROM laws WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/search')
def search():
    q = request.args.get('q', '')
    conn = get_db()
    laws = conn.execute(
        'SELECT * FROM laws WHERE title LIKE ? OR number LIKE ? OR description LIKE ?',
        (f'%{q}%', f'%{q}%', f'%{q}%')
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in laws])

@app.route('/ask')
def ask():
    return render_template('ask.html')

@app.route('/api/ask', methods=['POST'])
def api_ask():
    data = request.json
    question = data.get('question', '').lower()
    
    keywords = [w for w in question.split() if len(w) > 3]
    
    conn = get_db()
    results = []
    
    for keyword in keywords:
        articles = conn.execute('''
            SELECT a.*, l.title as law_title, l.number as law_number
            FROM articles a
            JOIN laws l ON a.law_id = l.id
            WHERE LOWER(a.content) LIKE ? OR LOWER(a.title) LIKE ?
        ''', (f'%{keyword}%', f'%{keyword}%')).fetchall()
        
        for article in articles:
            article_dict = dict(article)
            if not any(r['id'] == article_dict['id'] for r in results):
                results.append(article_dict)
    
    conn.close()
    return jsonify(results[:5])
if __name__ == '__main__':
    app.run(debug=True)