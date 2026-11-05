# SellTrack - sales tracker for any product

A Flask + SQLite website where each person makes an account, saves their products and costs,
records sales, and splits the profit.

## Folder layout

```
sales_tracker/
|-- app.py                # main flask app
|-- templates/
|   |-- base.html         # shared header / nav
|   |-- login.html        # log in
|   |-- register.html     # create an account
|   |-- home.html         # totals + add a sale
|   |-- sales.html        # all sales (search, edit, delete)
|   |-- edit.html         # edit one sale
|   |-- products.html     # your products + costs
|   |-- product_edit.html # change a cost + cost history
|   |-- split.html        # profit split
|   `-- _sale_form.html   # the sale form (used by home + edit)
`-- static/
    |-- styles.css
    `-- logo.svg
```

The database (`tracker.db`) is created automatically in an `instance` folder the first time you run the app.

## How to run

1. `pip install flask flask_sqlalchemy`
2. `python app.py`
3. Open http://127.0.0.1:5000, create an account and add a product

## Database tables

- **User** - username + hashed password
- **Product** - name, cost, selling price (belongs to a user)
- **CostChange** - diary of every cost change for a product
- **Sale** - date, customer, product, quantity, price paid, cost at the time of sale
- **Partner** - name + % share for the profit split

Every product, sale and partner is linked to a `user_id`, so people only ever see their own data.

## How cost updates work

When you save a sale, the product's cost at that moment is copied into the sale (`cost_each`).
If you change the product's cost later, old sales keep their old cost and only new sales use the new one.
