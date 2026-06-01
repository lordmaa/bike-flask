import re
import requests
from flask import Blueprint, jsonify, request, render_template
from database import get_db, query_db

bp = Blueprint('nutrition', __name__, url_prefix='/nutrition')

MEAL_KEYS = ['breakfast', 'lunch', 'dinner', 'snacks', 'ride_fuel', 'recovery']


def _default_rider_id():
    row = query_db('SELECT id FROM Rider WHERE isDefault=1 LIMIT 1', one=True)
    return row['id'] if row else None


def _parse_serving_g(s):
    if not s:
        return None
    m = re.search(r'(\d+(?:\.\d+)?)\s*g', s, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _get_goals():
    row = query_db('''
        SELECT nutritionCalGoal, nutritionProteinGoal, nutritionCarbGoal,
               nutritionFatGoal, nutritionWaterGoalMl, nutritionBmrKcal
        FROM Settings WHERE id=1
    ''', one=True)
    if not row:
        return {}
    return {
        'cal':     row['nutritionCalGoal'],
        'protein': row['nutritionProteinGoal'],
        'carbs':   row['nutritionCarbGoal'],
        'fat':     row['nutritionFatGoal'],
        'water':   row['nutritionWaterGoalMl'],
        'bmr':     row['nutritionBmrKcal'],
    }


def _apply_override(product):
    barcode = product.get('barcode')
    if not barcode:
        return product
    ov = query_db('SELECT * FROM FoodOverride WHERE barcode=?', [barcode], one=True)
    if not ov:
        return product
    return {
        **product,
        'name':             ov['name'],
        'brand':            ov['brand'] or product.get('brand', ''),
        'kcal_per_100g':    ov['kcal100g'],
        'protein_per_100g': ov['protein100g'],
        'carbs_per_100g':   ov['carbs100g'],
        'fat_per_100g':     ov['fat100g'],
        'serving_g':        ov['servingG'] or product.get('serving_g'),
        '_overridden':      True,
    }


def _mqtt_nutrition():
    try:
        from services.mqtt import push_update_nutrition
        push_update_nutrition()
    except Exception:
        pass


# ── Page ──────────────────────────────────────────────────────────────

@bp.route('/')
def index():
    return render_template('nutrition.html')


# ── Goals ─────────────────────────────────────────────────────────────

@bp.route('/api/goals', methods=['GET'])
def get_goals():
    return jsonify(_get_goals())


@bp.route('/api/goals', methods=['POST'])
def save_goals():
    d = request.get_json()
    db = get_db()
    db.execute('INSERT OR IGNORE INTO Settings (id) VALUES (1)')
    db.execute('''
        UPDATE Settings SET
            nutritionCalGoal=?, nutritionProteinGoal=?, nutritionCarbGoal=?,
            nutritionFatGoal=?, nutritionWaterGoalMl=?, nutritionBmrKcal=?
        WHERE id=1
    ''', [
        d.get('cal')     or None,
        d.get('protein') or None,
        d.get('carbs')   or None,
        d.get('fat')     or None,
        d.get('water')   or None,
        d.get('bmr')     or None,
    ])
    db.commit()
    return jsonify({'ok': True})


# ── OFFs search + lookup ──────────────────────────────────────────────

@bp.route('/api/search')
def search_food():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    try:
        resp = requests.get(
            'https://world.openfoodfacts.org/cgi/search.pl',
            params={
                'search_terms':   q,
                'search_simple':  1,
                'action':         'process',
                'json':           1,
                'page_size':      12,
                'fields':         'code,product_name,product_name_en,brands,nutriments,serving_size',
                'countries_tags': 'en:united-kingdom',
            },
            timeout=8,
            headers={'User-Agent': 'Headwind-Nutrition/1.0'},
        )
        if not resp.ok:
            return jsonify([])
        products = resp.json().get('products', [])
        results = []
        for p in products:
            name = (p.get('product_name_en') or p.get('product_name') or '').strip()
            if not name:
                continue
            n = p.get('nutriments', {})
            kcal = n.get('energy-kcal_100g')
            if not kcal:
                continue
            product = {
                'barcode':          p.get('code', ''),
                'name':             name,
                'brand':            (p.get('brands') or '').split(',')[0].strip(),
                'kcal_per_100g':    kcal,
                'protein_per_100g': n.get('proteins_100g'),
                'carbs_per_100g':   n.get('carbohydrates_100g'),
                'fat_per_100g':     n.get('fat_100g'),
                'serving_g':        _parse_serving_g(p.get('serving_size', '')),
            }
            results.append(_apply_override(product))
        return jsonify(results)
    except Exception:
        return jsonify([])


@bp.route('/api/lookup/<barcode>')
def lookup(barcode):
    ov = query_db('SELECT * FROM FoodOverride WHERE barcode=?', [barcode], one=True)
    if ov:
        return jsonify({
            'name':             ov['name'],
            'brand':            ov['brand'] or '',
            'kcal_per_100g':    ov['kcal100g'],
            'protein_per_100g': ov['protein100g'],
            'carbs_per_100g':   ov['carbs100g'],
            'fat_per_100g':     ov['fat100g'],
            'serving_g':        ov['servingG'],
            '_overridden':      True,
        })
    url = f'https://world.openfoodfacts.org/api/v2/product/{barcode}.json'
    try:
        resp = requests.get(url, timeout=8, headers={'User-Agent': 'Headwind-Nutrition/1.0'})
        if not resp.ok:
            return jsonify({'error': 'not_found'}), 404
        data = resp.json()
        if data.get('status') != 1:
            return jsonify({'error': 'not_found'}), 404
        p = data['product']
        n = p.get('nutriments', {})
        return jsonify({
            'name':             (p.get('product_name_en') or p.get('product_name') or '').strip() or 'Unknown product',
            'brand':            p.get('brands', '').split(',')[0].strip(),
            'kcal_per_100g':    n.get('energy-kcal_100g'),
            'protein_per_100g': n.get('proteins_100g'),
            'carbs_per_100g':   n.get('carbohydrates_100g'),
            'fat_per_100g':     n.get('fat_100g'),
            'serving_g':        _parse_serving_g(p.get('serving_size', '')),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Log entries ───────────────────────────────────────────────────────

@bp.route('/api/log', methods=['POST'])
def log_food():
    d = request.get_json()
    rider_id = _default_rider_id()
    db = get_db()
    db.execute('''
        INSERT INTO FoodLog
            (riderId, logDate, barcode, foodName, calories, protein, carbs, fat,
             servingG, quantity, mealType, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', [
        rider_id, d['date'], d.get('barcode'), d['name'],
        d.get('calories'), d.get('protein'), d.get('carbs'), d.get('fat'),
        d.get('serving_g'), d.get('quantity', 1),
        d.get('meal_type', 'uncategorised'),
        d.get('source', 'manual'),
    ])
    db.commit()
    entry_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    _mqtt_nutrition()
    return jsonify({'ok': True, 'id': entry_id})


@bp.route('/api/log/<int:entry_id>', methods=['DELETE'])
def delete_log(entry_id):
    db = get_db()
    db.execute('DELETE FROM FoodLog WHERE id=?', [entry_id])
    db.commit()
    _mqtt_nutrition()
    return jsonify({'ok': True})


# ── Water ─────────────────────────────────────────────────────────────

@bp.route('/api/water', methods=['POST'])
def add_water():
    d = request.get_json()
    rider_id = _default_rider_id()
    db = get_db()
    db.execute(
        'INSERT INTO HydrationLog (riderId, logDate, ml) VALUES (?, ?, ?)',
        [rider_id, d['date'], int(d['ml'])],
    )
    db.commit()
    entry_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    _mqtt_nutrition()
    return jsonify({'ok': True, 'id': entry_id})


@bp.route('/api/water/<int:entry_id>', methods=['DELETE'])
def delete_water(entry_id):
    db = get_db()
    db.execute('DELETE FROM HydrationLog WHERE id=?', [entry_id])
    db.commit()
    _mqtt_nutrition()
    return jsonify({'ok': True})


# ── Day summary ───────────────────────────────────────────────────────

@bp.route('/api/day/<date_str>')
def day_log(date_str):
    rider_id = _default_rider_id()
    entries = query_db('''
        SELECT id, foodName, calories, protein, carbs, fat, servingG, quantity,
               barcode, mealType, source
        FROM FoodLog WHERE riderId=? AND logDate=? ORDER BY createdAt ASC
    ''', [rider_id, date_str])
    water_row = query_db(
        'SELECT COALESCE(SUM(ml),0) as total, MAX(id) as lastId FROM HydrationLog WHERE riderId=? AND logDate=?',
        [rider_id, date_str], one=True,
    )
    water_ml   = int(water_row['total'])  if water_row else 0
    last_water = water_row['lastId']      if water_row else None
    garmin_row = query_db(
        'SELECT totalCalories, activeCalories FROM GarminDaily WHERE date=?',
        [date_str], one=True,
    )
    garmin_total  = garmin_row['totalCalories']  if garmin_row else None
    garmin_active = garmin_row['activeCalories'] if garmin_row else None
    ride_row = query_db('''
        SELECT COALESCE(SUM(calories), 0) as total FROM Activity
        WHERE riderId=? AND date(startDateLocal)=? AND calories IS NOT NULL
    ''', [rider_id, date_str], one=True)
    ride_cal = int(ride_row['total']) if ride_row else 0

    goals = _get_goals()
    bmr   = goals.get('bmr')

    # Burn calculation priority:
    #   1. Manual BMR + today's ride calories (always current, no sync lag)
    #   2. Garmin daily total (comprehensive but up to 2h stale)
    #   3. Ride calories only
    if bmr:
        burned      = int(bmr) + ride_cal
        burn_source = 'BMR + rides' if ride_cal else 'BMR (resting)'
    elif garmin_total:
        burned      = garmin_total
        burn_source = 'Garmin total'
    elif ride_cal:
        burned      = ride_cal
        burn_source = 'rides'
    else:
        burned      = None
        burn_source = None

    return jsonify({
        'entries':       [dict(e) for e in entries],
        'water_ml':      water_ml,
        'last_water_id': last_water,
        'ride_calories': ride_cal,
        'garmin_total':  garmin_total,
        'garmin_active': garmin_active,
        'burned':        burned,
        'burn_source':   burn_source,
        'goals':         goals,
    })


# ── Recent foods ──────────────────────────────────────────────────────

@bp.route('/api/recent')
def recent_foods():
    rider_id = _default_rider_id()
    rows = query_db('''
        SELECT foodName, barcode, calories, protein, carbs, fat, servingG, quantity
        FROM FoodLog
        WHERE riderId=? AND calories IS NOT NULL
        GROUP BY LOWER(TRIM(foodName))
        ORDER BY MAX(createdAt) DESC
        LIMIT 20
    ''', [rider_id])
    return jsonify([dict(r) for r in rows])


# ── Saved meals ───────────────────────────────────────────────────────

@bp.route('/api/saved-meals', methods=['GET'])
def list_saved_meals():
    meals = query_db('SELECT id, name, createdAt FROM SavedMeal ORDER BY name ASC')
    result = []
    for m in meals:
        items = query_db(
            'SELECT id, foodName, calories, protein, carbs, fat, servingG, barcode '
            'FROM SavedMealItem WHERE mealId=? ORDER BY id',
            [m['id']],
        )
        result.append({
            'id':        m['id'],
            'name':      m['name'],
            'createdAt': m['createdAt'],
            'items':     [dict(i) for i in items],
            'totalCal':  round(sum(i['calories'] or 0 for i in items)),
        })
    return jsonify(result)


@bp.route('/api/saved-meals', methods=['POST'])
def create_saved_meal():
    d = request.get_json()
    name  = (d.get('name') or 'Saved Meal').strip()[:100]
    items = d.get('items') or []
    if not items:
        return jsonify({'error': 'No items provided'}), 400
    db = get_db()
    cur = db.execute('INSERT INTO SavedMeal (name) VALUES (?)', [name])
    meal_id = cur.lastrowid
    for item in items:
        db.execute('''
            INSERT INTO SavedMealItem (mealId, foodName, calories, protein, carbs, fat, servingG, barcode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', [
            meal_id, item['foodName'],
            item.get('calories'), item.get('protein'), item.get('carbs'),
            item.get('fat'), item.get('servingG'), item.get('barcode'),
        ])
    db.commit()
    return jsonify({'id': meal_id, 'name': name}), 201


@bp.route('/api/saved-meals/<int:meal_id>', methods=['DELETE'])
def delete_saved_meal(meal_id):
    db = get_db()
    db.execute('DELETE FROM SavedMealItem WHERE mealId=?', [meal_id])
    db.execute('DELETE FROM SavedMeal WHERE id=?', [meal_id])
    db.commit()
    return '', 204


@bp.route('/api/saved-meals/<int:meal_id>/log', methods=['POST'])
def log_saved_meal(meal_id):
    d         = request.get_json()
    rider_id  = _default_rider_id()
    date_str  = d.get('date')
    meal_type = d.get('meal_type', 'uncategorised')
    scale     = float(d.get('scale', 1.0))
    items = query_db(
        'SELECT * FROM SavedMealItem WHERE mealId=?', [meal_id]
    )
    if not items:
        return jsonify({'error': 'Meal not found or empty'}), 404
    db = get_db()
    for item in items:
        db.execute('''
            INSERT INTO FoodLog
                (riderId, logDate, barcode, foodName, calories, protein, carbs, fat,
                 servingG, quantity, mealType, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'saved_meal')
        ''', [
            rider_id, date_str, item['barcode'], item['foodName'],
            (item['calories'] * scale) if item['calories'] else None,
            (item['protein']  * scale) if item['protein']  else None,
            (item['carbs']    * scale) if item['carbs']    else None,
            (item['fat']      * scale) if item['fat']      else None,
            (item['servingG'] * scale) if item['servingG'] else None,
            meal_type,
        ])
    db.commit()
    _mqtt_nutrition()
    return jsonify({'ok': True})


# ── Copy day ──────────────────────────────────────────────────────────

@bp.route('/api/copy-day', methods=['POST'])
def copy_day():
    d         = request.get_json()
    from_date = d.get('from_date')
    to_date   = d.get('to_date')
    meal_type = d.get('meal_type')
    rider_id  = _default_rider_id()
    if meal_type:
        entries = query_db(
            'SELECT * FROM FoodLog WHERE riderId=? AND logDate=? AND mealType=?',
            [rider_id, from_date, meal_type],
        )
    else:
        entries = query_db(
            'SELECT * FROM FoodLog WHERE riderId=? AND logDate=?',
            [rider_id, from_date],
        )
    if not entries:
        return jsonify({'count': 0})
    db = get_db()
    for e in entries:
        db.execute('''
            INSERT INTO FoodLog
                (riderId, logDate, barcode, foodName, calories, protein, carbs, fat,
                 servingG, quantity, mealType, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'copied')
        ''', [
            rider_id, to_date, e['barcode'], e['foodName'],
            e['calories'], e['protein'], e['carbs'], e['fat'],
            e['servingG'], e['quantity'],
            e['mealType'] if e['mealType'] else 'uncategorised',
        ])
    db.commit()
    _mqtt_nutrition()
    return jsonify({'ok': True, 'count': len(entries)})


# ── Food overrides ────────────────────────────────────────────────────

@bp.route('/api/override', methods=['POST'])
def save_override():
    d       = request.get_json()
    barcode = (d.get('barcode') or '').strip()
    if not barcode:
        return jsonify({'error': 'barcode required'}), 400
    db = get_db()
    db.execute('''
        INSERT INTO FoodOverride
            (barcode, name, brand, kcal100g, protein100g, carbs100g, fat100g, servingG, updatedAt)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(barcode) DO UPDATE SET
            name=excluded.name, brand=excluded.brand,
            kcal100g=excluded.kcal100g, protein100g=excluded.protein100g,
            carbs100g=excluded.carbs100g, fat100g=excluded.fat100g,
            servingG=excluded.servingG, updatedAt=datetime('now')
    ''', [
        barcode, d.get('name'), d.get('brand'),
        d.get('kcal_per_100g'), d.get('protein_per_100g'),
        d.get('carbs_per_100g'), d.get('fat_per_100g'), d.get('serving_g'),
    ])
    db.commit()
    return jsonify({'ok': True})
