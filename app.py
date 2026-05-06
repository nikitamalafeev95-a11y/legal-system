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
    keywords = [w for w in question.split() if len(w) > 2]

    conn = get_db()
    results = []
    matched_laws = []

    for keyword in keywords:
        # Ищем в статьях
        articles = conn.execute('''
            SELECT a.*, l.title as law_title, l.number as law_number, l.description as law_desc
            FROM articles a
            JOIN laws l ON a.law_id = l.id
            WHERE LOWER(a.content) LIKE ? OR LOWER(a.title) LIKE ?
        ''', (f'%{keyword}%', f'%{keyword}%')).fetchall()

        for article in articles:
            article_dict = dict(article)
            if not any(r['id'] == article_dict['id'] for r in results):
                results.append(article_dict)

        # Ищем в самих законах
        laws = conn.execute('''
            SELECT * FROM laws
            WHERE LOWER(title) LIKE ? OR LOWER(description) LIKE ? OR LOWER(number) LIKE ?
        ''', (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%')).fetchall()

        for law in laws:
            law_dict = dict(law)
            if not any(l['id'] == law_dict['id'] for l in matched_laws):
                matched_laws.append(law_dict)

                # Добавляем статьи этого закона в результаты
                law_articles = conn.execute('''
                    SELECT a.*, l.title as law_title, l.number as law_number, l.description as law_desc
                    FROM articles a
                    JOIN laws l ON a.law_id = l.id
                    WHERE a.law_id = ?
                ''', (law_dict['id'],)).fetchall()

                for article in law_articles:
                    article_dict = dict(article)
                    if not any(r['id'] == article_dict['id'] for r in results):
                        results.append(article_dict)

    conn.close()

    # Генерация краткого ответа
    answer = None
    if matched_laws:
        # Если нашли сам закон — объясняем что это
        law = matched_laws[0]
        answer = f"{law['number']} «{law['title']}» ({law['year']}) — {law['description']}"
        if len(matched_laws) > 1:
            others = ', '.join([f"{l['number']}" for l in matched_laws[1:3]])
            answer += f" Также найдены связанные документы: {others}."
    elif results:
        # Иначе — извлекаем релевантные предложения
        excerpts = []
        for r in results[:3]:
            content = r['content']
            sentences = content.replace('!', '.').replace('?', '.').split('.')
            for sentence in sentences:
                sentence = sentence.strip()
                if any(kw in sentence.lower() for kw in keywords) and len(sentence) > 30:
                    excerpts.append(sentence)
                    break
            else:
                if len(content) > 50:
                    excerpts.append(content[:200])
        if excerpts:
            answer = 'На основании найденных нормативных актов: ' + ' '.join(excerpts[:2])

    return jsonify({
        'articles': results[:6],
        'answer': answer,
        'keywords': keywords
    })