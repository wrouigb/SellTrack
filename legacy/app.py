# SellTrack - a sales tracker for any product
# Run with:  python app.py   then open http://127.0.0.1:5000

import os
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, flash, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# ---------- Create Flask app ----------
app = Flask(__name__)
# the secret key keeps the login sessions safe - change it to your own random text
app.secret_key = os.environ.get("SECRET_KEY", "change-this-to-your-own-secret-text")
app.permanent_session_lifetime = timedelta(days=7)   # stay logged in for a week

# ---------- Configure SQLite database ----------
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///tracker.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# ---------- Initialize SQLAlchemy ----------
db = SQLAlchemy(app)


# =====================================================
#   DATABASE TABLES (models)
# =====================================================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(30), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)   # we never store the real password


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)   # whose product
    name = db.Column(db.String(100), nullable=False)
    cost = db.Column(db.Float, nullable=False)          # what ONE item costs me right now
    sell_price = db.Column(db.Float)                    # what I usually sell ONE item for (optional)


class CostChange(db.Model):
    """A diary of every time a product's cost was set or changed"""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    old_cost = db.Column(db.Float)                      # empty for the very first cost
    new_cost = db.Column(db.Float, nullable=False)
    changed_on = db.Column(db.DateTime, default=datetime.now)


class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))   # empty if product deleted later
    product_name = db.Column(db.String(100), nullable=False)          # saved so old sales still make sense
    sale_date = db.Column(db.Date, nullable=False)
    customer = db.Column(db.String(100))
    quantity = db.Column(db.Integer, nullable=False)
    price_paid = db.Column(db.Float, nullable=False)    # total $ the customer paid
    cost_each = db.Column(db.Float, nullable=False)     # the cost at the time of sale (never changes after!)

    def total_cost(self):
        return self.quantity * self.cost_each

    def profit(self):
        return self.price_paid - self.total_cost()


class Partner(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    share = db.Column(db.Float, nullable=False)         # percent of the profit (0-100)


# =====================================================
#   HELPER FUNCTIONS
# =====================================================
@app.template_filter('money')
def money(value):
    """Shows a number like $12.50 (or -$3.00 for a loss)"""
    if value < 0:
        return "-${:,.2f}".format(abs(value))
    return "${:,.2f}".format(value)


def login_required(view):
    """Put @login_required under a route to make people log in first"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session or db.session.get(User, session['user_id']) is None:
            session.clear()
            flash("Please log in first.", "error")
            return redirect('/login')
        return view(*args, **kwargs)
    return wrapped


def get_totals(sales):
    """Adds up a list of sales"""
    received = sum(s.price_paid for s in sales)
    cost = sum(s.total_cost() for s in sales)
    return {"stamps": sum(s.quantity for s in sales),    # total items sold
            "received": received, "cost": cost, "profit": received - cost,
            "count": len(sales)}


def get_by_product(sales):
    """Groups sales by product so we can show profit for each one"""
    groups = {}
    for s in sales:
        g = groups.setdefault(s.product_name,
                              {"name": s.product_name, "qty": 0, "received": 0, "profit": 0})
        g["qty"] += s.quantity
        g["received"] += s.price_paid
        g["profit"] += s.profit()
    return sorted(groups.values(), key=lambda g: g["profit"], reverse=True)


def read_sale_form():
    """Reads the sale form. Returns a dictionary, or None if something is wrong."""
    try:
        sale_date = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
        quantity = int(request.form['quantity'])
        price_paid = float(request.form['price_paid'])
    except (ValueError, KeyError):
        return None

    if quantity <= 0 or price_paid < 0:
        return None

    return {"sale_date": sale_date,
            "customer": request.form.get('customer', '').strip(),
            "quantity": quantity,
            "price_paid": price_paid}


def read_product_form():
    """Reads the product form. Returns a dictionary, or None if something is wrong."""
    name = request.form.get('name', '').strip()
    try:
        cost = float(request.form['cost'])
        sell_text = request.form.get('sell_price', '').strip()
        sell_price = float(sell_text) if sell_text else None    # selling price is optional
    except (ValueError, KeyError):
        return None

    if name == "" or cost < 0 or (sell_price is not None and sell_price < 0):
        return None
    return {"name": name, "cost": cost, "sell_price": sell_price}


def name_is_taken(name, user_id, ignore_id=None):
    """True if this user already has a product with this name"""
    query = Product.query.filter(db.func.lower(Product.name) == name.lower(),
                                 Product.user_id == user_id)
    if ignore_id is not None:
        query = query.filter(Product.id != ignore_id)
    return query.first() is not None


# =====================================================
#   LOGIN / REGISTER / LOGOUT
# =====================================================
@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect('/')

    username = ""
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')

        error = None
        if len(username) < 3 or len(username) > 30:
            error = "Username must be 3 to 30 characters."
        elif not username.replace('_', '').replace('.', '').replace('-', '').isalnum():
            error = "Username can only use letters, numbers, _ . and -"
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm:
            error = "The two passwords don't match."
        elif User.query.filter_by(username=username).first():
            error = "That username is already taken."

        if error:
            flash(error, "error")
        else:
            user = User(username=username, password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            session.clear()
            session.permanent = True
            session['user_id'] = user.id
            session['username'] = user.username
            flash("Account created! Start by adding a product below.", "good")
            return redirect('/products')

    return render_template('register.html', username=username)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect('/')

    username = ""
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()

        if user is not None and check_password_hash(user.password_hash, password):
            session.clear()
            session.permanent = True
            session['user_id'] = user.id
            session['username'] = user.username
            return redirect('/')
        flash("Wrong username or password.", "error")

    return render_template('login.html', username=username)


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash("You are logged out.", "good")
    return redirect('/login')


# =====================================================
#   HOME PAGE: summary + add a sale
# =====================================================
@app.route('/', methods=['GET', 'POST'])
@login_required
def home():
    uid = session['user_id']
    products = Product.query.filter_by(user_id=uid).order_by(Product.name).all()

    if request.method == 'POST':
        data = read_sale_form()

        # find the product (and make sure it belongs to this user)
        product = None
        try:
            product = Product.query.filter_by(id=int(request.form['product_id']), user_id=uid).first()
        except (ValueError, KeyError):
            pass

        if data is None or product is None:
            flash("Couldn't save that sale - check the product, date, quantity and price.", "error")
        else:
            # the cost is taken from the product at THIS moment and saved with the sale
            sale = Sale(user_id=uid, product_id=product.id, product_name=product.name,
                        cost_each=product.cost, **data)
            db.session.add(sale)
            db.session.commit()
            flash("Sale saved!", "good")
        return redirect('/')   # redirect so refreshing doesn't re-submit

    sales = Sale.query.filter_by(user_id=uid).all()
    recent = Sale.query.filter_by(user_id=uid).order_by(Sale.sale_date.desc(), Sale.id.desc()).limit(5).all()
    return render_template('home.html', totals=get_totals(sales), by_product=get_by_product(sales),
                           recent=recent, products=products, today=date.today().isoformat())


# =====================================================
#   ALL SALES (search, edit, delete)
# =====================================================
@app.route('/sales')
@login_required
def sales():
    search = request.args.get('q', '').strip()
    query = Sale.query.filter_by(user_id=session['user_id'])
    if search:
        query = query.filter(Sale.customer.contains(search) | Sale.product_name.contains(search))
    all_sales = query.order_by(Sale.sale_date.desc(), Sale.id.desc()).all()

    shown = get_totals(all_sales)   # totals for just the sales being shown
    return render_template('sales.html', sales=all_sales, shown=shown, search=search)


@app.route('/edit/<int:sale_id>', methods=['GET', 'POST'])
@login_required
def edit(sale_id):
    sale = Sale.query.filter_by(id=sale_id, user_id=session['user_id']).first_or_404()
    if request.method == 'POST':
        data = read_sale_form()
        if data is None:
            flash("Couldn't update - check the date, quantity and price.", "error")
            return redirect(f'/edit/{sale_id}')
        for key, value in data.items():
            setattr(sale, key, value)
        db.session.commit()
        flash("Sale updated.", "good")
        return redirect('/sales')
    return render_template('edit.html', sale=sale)


@app.route('/delete/<int:sale_id>', methods=['POST'])
@login_required
def delete(sale_id):
    sale = Sale.query.filter_by(id=sale_id, user_id=session['user_id']).first_or_404()
    db.session.delete(sale)
    db.session.commit()
    flash("Sale deleted.", "good")
    return redirect('/sales')


# =====================================================
#   PRODUCTS PAGE: save products and update their cost
# =====================================================
@app.route('/products', methods=['GET', 'POST'])
@login_required
def products():
    uid = session['user_id']

    if request.method == 'POST':
        data = read_product_form()
        if data is None:
            flash("Enter a product name and a cost (selling price is optional).", "error")
        elif name_is_taken(data["name"], uid):
            flash("You already have a product with that name.", "error")
        else:
            product = Product(user_id=uid, **data)
            db.session.add(product)
            db.session.commit()
            # write the first line of the cost diary
            db.session.add(CostChange(product_id=product.id, old_cost=None, new_cost=product.cost))
            db.session.commit()
            flash(f"{product.name} added.", "good")
        return redirect('/products')

    all_products = Product.query.filter_by(user_id=uid).order_by(Product.name).all()
    sale_counts = {p.id: Sale.query.filter_by(product_id=p.id).count() for p in all_products}
    return render_template('products.html', products=all_products, sale_counts=sale_counts)


@app.route('/products/<int:product_id>', methods=['GET', 'POST'])
@login_required
def edit_product(product_id):
    uid = session['user_id']
    product = Product.query.filter_by(id=product_id, user_id=uid).first_or_404()

    if request.method == 'POST':
        data = read_product_form()
        if data is None:
            flash("Enter a product name and a cost (selling price is optional).", "error")
            return redirect(f'/products/{product_id}')
        if name_is_taken(data["name"], uid, ignore_id=product.id):
            flash("You already have a product with that name.", "error")
            return redirect(f'/products/{product_id}')

        # if the cost changed, write it in the diary.
        # Old sales keep their old cost - only NEW sales use the new cost.
        if abs(data["cost"] - product.cost) > 0.00001:
            db.session.add(CostChange(product_id=product.id, old_cost=product.cost,
                                      new_cost=data["cost"]))

        # if the name changed, update it on this product's old sales too
        if data["name"] != product.name:
            Sale.query.filter_by(product_id=product.id).update({"product_name": data["name"]})

        product.name = data["name"]
        product.cost = data["cost"]
        product.sell_price = data["sell_price"]
        db.session.commit()
        flash("Product updated. New sales will use the new cost.", "good")
        return redirect('/products')

    history = CostChange.query.filter_by(product_id=product.id).order_by(CostChange.changed_on.desc(),
                                                                         CostChange.id.desc()).all()
    return render_template('product_edit.html', product=product, history=history)


@app.route('/products/delete/<int:product_id>', methods=['POST'])
@login_required
def delete_product(product_id):
    product = Product.query.filter_by(id=product_id, user_id=session['user_id']).first_or_404()
    # old sales stay in the records, they just lose the link to the product
    Sale.query.filter_by(product_id=product.id).update({"product_id": None})
    CostChange.query.filter_by(product_id=product.id).delete()
    db.session.delete(product)
    db.session.commit()
    flash("Product deleted. Your past sales were kept.", "good")
    return redirect('/products')


# =====================================================
#   PROFIT SPLIT PAGE
# =====================================================
@app.route('/split', methods=['GET', 'POST'])
@login_required
def split():
    uid = session['user_id']

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        try:
            share = float(request.form['share'])
        except (ValueError, KeyError):
            share = -1
        if name == "" or share < 0 or share > 100:
            flash("Enter a name and a share between 0 and 100.", "error")
        else:
            db.session.add(Partner(user_id=uid, name=name, share=share))
            db.session.commit()
            flash(f"{name} added.", "good")
        return redirect('/split')

    partners = Partner.query.filter_by(user_id=uid).all()
    totals = get_totals(Sale.query.filter_by(user_id=uid).all())
    total_share = sum(p.share for p in partners)

    # work out how much each person gets
    rows = []
    for p in partners:
        rows.append({"partner": p, "amount": totals["profit"] * p.share / 100})

    return render_template('split.html', rows=rows, totals=totals, total_share=total_share)


@app.route('/split/even', methods=['POST'])
@login_required
def split_even():
    """Make everyone's share equal"""
    partners = Partner.query.filter_by(user_id=session['user_id']).all()
    if partners:
        for p in partners:
            p.share = round(100 / len(partners), 2)
        db.session.commit()
        flash("Shares set to an even split.", "good")
    return redirect('/split')


@app.route('/split/delete/<int:partner_id>', methods=['POST'])
@login_required
def delete_partner(partner_id):
    partner = Partner.query.filter_by(id=partner_id, user_id=session['user_id']).first_or_404()
    db.session.delete(partner)
    db.session.commit()
    flash("Person removed.", "good")
    return redirect('/split')


# ---------- Run the app ----------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()   # makes the database + tables if they don't exist
    app.run(debug=True)
