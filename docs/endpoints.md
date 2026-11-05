# Endpoints

Every address the app answers. There's no public API: these are pages and form posts for the app itself, and every `POST` needs a CSRF token. A test checks that this list matches the app.

- **Anyone** can reach it without logging in.
- **Account** needs a login. Records belonging to another account are a 404, exactly as if they didn't exist.
- **Admin** needs an admin login. Anyone else gets a 403, and guests are sent to log in.

Forms with a mistake in them come back as a 422. Someone with a temporary password can only reach the page to change it.

## Public

| Method | Address | Who | What it does |
|---|---|---|---|
| GET | `/` | Anyone | Landing page, or the dashboard if you're logged in |
| GET | `/help` | Anyone | Help, how the split works, and the support form if you're logged in |
| GET | `/privacy` | Anyone | Privacy page |
| GET | `/terms` | Anyone | Terms page |
| GET | `/sales` | Anyone | Redirect from the original app's address |
| GET | `/edit/<int:sale_id>` | Anyone | Redirect from the original app's address |
| GET | `/products` | Anyone | Redirect from the original app's address |
| GET | `/products/<int:product_id>` | Anyone | Redirect from the original app's address |
| GET | `/split` | Anyone | Redirect from the original app's address |
| GET | `/static/<path:filename>` | Anyone | Built CSS, JavaScript, fonts and images. In production, Nginx serves `/static/dist/` and refuses the rest |

## Accounts

| Method | Address | Who | What it does |
|---|---|---|---|
| GET, POST | `/register` | Anyone | Create an account, then see recovery codes once. Rate limited |
| GET, POST | `/login` | Anyone | Log in. Rate limited by address and by username |
| POST | `/logout` | Anyone | Log out |
| GET, POST | `/recover` | Anyone | Set a new password with a recovery code. Rate limited |

## First steps

| Method | Address | Who | What it does |
|---|---|---|---|
| GET, POST | `/onboarding` | Account | Choose a time zone |
| GET, POST | `/onboarding/people` | Account | Add who shares the profit |
| POST | `/onboarding/people/even` | Account | Split shares evenly |
| GET, POST | `/onboarding/product` | Account | Add a first product |
| GET, POST | `/onboarding/sale` | Account | Record a first sale |
| POST | `/onboarding/finish` | Account | Finish, or skip the rest |

## Dashboard and sales

| Method | Address | Who | What it does |
|---|---|---|---|
| GET | `/app` | Account | Dashboard: `period`, `date_from` and `date_to` choose the dates |
| GET | `/app/sales` | Account | Sales: `q`, `date_from`, `date_to`, `product`, `received_by`, `sort`, `dir`, `page` |
| GET, POST | `/app/sales/new` | Account | Add a sale: `product` chooses one in advance |
| GET, POST | `/app/sales/<int:sale_id>/edit` | Account | Change a sale. A settled sale's money is locked |
| POST | `/app/sales/<int:sale_id>/delete` | Account | Delete a sale, which can be undone |
| POST | `/app/sales/bulk-delete` | Account | Delete several, skipping settled ones |
| POST | `/app/sales/restore` | Account | Undo a delete |
| GET | `/app/sales/export.csv` | Account | Download whatever the same filters show |

## Products

| Method | Address | Who | What it does |
|---|---|---|---|
| GET, POST | `/app/products` | Account | List and add products: `q`, `archived` |
| GET, POST | `/app/products/<int:product_id>` | Account | See and change a product, with its cost history |
| POST | `/app/products/<int:product_id>/archive` | Account | Archive it |
| POST | `/app/products/<int:product_id>/restore` | Account | Bring it back |
| POST | `/app/products/<int:product_id>/delete` | Account | Delete it |

## The split

| Method | Address | Who | What it does |
|---|---|---|---|
| GET | `/app/split` | Account | Who owes whom, right now |
| GET, POST | `/app/split/settle` | Account | Review and record a settle-up |
| GET, POST | `/app/split/costs` | Account | List and add upfront costs |
| GET, POST | `/app/split/costs/<int:cost_id>/edit` | Account | Change one |
| POST | `/app/split/costs/<int:cost_id>/delete` | Account | Delete one, unless it's settled |
| GET, POST | `/app/split/payments` | Account | List and record payments between people |
| GET, POST | `/app/split/payments/<int:payment_id>/edit` | Account | Change one |
| POST | `/app/split/payments/<int:payment_id>/delete` | Account | Delete one, unless it's settled |
| GET | `/app/split/history` | Account | Past settle-ups |
| GET | `/app/split/history/<int:settlement_id>` | Account | One settle-up's figures |
| POST | `/app/split/history/undo` | Account | Undo the latest settle-up |
| GET, POST | `/app/split/people` | Account | List and add people |
| POST | `/app/split/people/shares` | Account | Change names and shares |
| POST | `/app/split/people/even` | Account | Split shares evenly |
| POST | `/app/split/people/<int:partner_id>/usual-receiver` | Account | Set who usually receives the money |
| POST | `/app/split/people/<int:partner_id>/archive` | Account | Archive someone, once they've settled |
| POST | `/app/split/people/<int:partner_id>/restore` | Account | Bring someone back |
| POST | `/app/split/people/<int:partner_id>/delete` | Account | Delete someone with no history at all |

## Settings, notifications and support

| Method | Address | Who | What it does |
|---|---|---|---|
| GET | `/app/settings` | Account | Account details |
| GET | `/app/settings/security` | Account | Recovery codes, recent sign-ins |
| GET, POST | `/app/settings/security/password` | Account | Change your password. Rate limited |
| POST | `/app/settings/security/recovery-codes` | Account | Replace your recovery codes. Rate limited |
| POST | `/app/settings/security/logout-everywhere` | Account | End every session |
| GET, POST | `/app/settings/preferences` | Account | Time zone and appearance |
| GET, POST | `/app/settings/data` | Account | Download or delete your data. Rate limited |
| GET | `/app/settings/data/export.zip` | Account | A zip of everything in the account. Rate limited and audit-logged |
| GET | `/app/notifications` | Account | Notifications: `show=unread`, `page` |
| GET | `/app/notifications/bell` | Anyone | The bell's count. Has nothing to show a guest, and doesn't extend a session |
| POST | `/app/notifications/<int:notification_id>/read` | Account | Mark one as read |
| POST | `/app/notifications/<int:notification_id>/open` | Account | Mark one as read and go to it |
| POST | `/app/notifications/read-all` | Account | Mark everything as read |
| GET, POST | `/app/support` | Account | Your messages and replies, and sending a new one. Rate limited |

## Admin

| Method | Address | Who | What it does |
|---|---|---|---|
| GET | `/admin` | Admin | Overview |
| GET | `/admin/users` | Admin | Accounts: `q`, `status`, `role`, `sort`, `dir`, `page` |
| POST | `/admin/users/bulk-suspend` | Admin | Suspend several, never yourself |
| GET | `/admin/users/<int:user_id>` | Admin | One account's details, counts and activity |
| POST | `/admin/users/<int:user_id>/suspend` | Admin | Suspend it, which ends its sessions |
| POST | `/admin/users/<int:user_id>/reactivate` | Admin | Let it log in again |
| POST | `/admin/users/<int:user_id>/reset-password` | Admin | Show a temporary password, once |
| GET | `/admin/support` | Admin | Support inbox: `status`, `q`, `page` |
| GET, POST | `/admin/support/<int:message_id>` | Admin | Read and reply |
| POST | `/admin/support/<int:message_id>/close` | Admin | Close a message |
| POST | `/admin/support/<int:message_id>/reopen` | Admin | Reopen a message |
| GET, POST | `/admin/announcements` | Admin | Send an announcement, and see past ones |
| GET | `/admin/audit` | Admin | Audit log: `action`, `actor`, `date_from`, `date_to`, `page` |
| GET, POST | `/admin/flags` | Admin | List and add feature flags |
| GET, POST | `/admin/flags/<int:flag_id>` | Admin | Change a flag |
| POST | `/admin/flags/<int:flag_id>/delete` | Admin | Delete a flag |
| GET | `/admin/system` | Admin | Database, Redis, disk, last backup and last cleanup |

## Health

| Method | Address | Who | What it does |
|---|---|---|---|
| GET | `/healthz` | Anyone | Is the process up? Checks nothing else |
| GET | `/readyz` | Anyone | Can it serve requests? A 503 if the database is down. Redis being down doesn't count |
