# AC Repair and Service Management System

A responsive Flask/MySQL application for customer repair bookings, technician job management, and administrator operations. The interface uses Bootstrap 5, with role-scoped workflows and server-side validation.

## Requirements

- Python 3.10 or newer
- MySQL 8.0 or newer, with a local database user
- Internet access in the browser for Bootstrap, Bootstrap Icons, Google Fonts, and the Unsplash hero photography

## Project Layout

```text
ac_repair_system/
├── app.py
├── api/index.py
├── config.py
├── vercel.json
├── schema.sql
├── requirements.txt
├── README.md
├── .env.example
├── .gitignore
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── register.html
│   ├── login.html
│   ├── book_service.html
│   ├── track_service.html
│   ├── my_requests.html
│   ├── contact.html
│   ├── customer_dashboard.html
│   ├── technician_dashboard.html
│   ├── admin_dashboard.html
│   ├── manage_requests.html
│   ├── manage_technicians.html
│   ├── manage_services.html
│   └── error.html
└── static/
    ├── css/style.css
    ├── js/script.js
    └── images/
```

The schema includes `users`, `technicians`, `services`, `service_requests`, `payments`, and `contact_messages`. Payment records are separate so revenue counts only payments marked `Received`.

## Install on Windows

Open PowerShell in this project directory (`ac_repair_system`):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

If PowerShell blocks virtual-environment activation, run the commands below using `.venv\Scripts\python.exe` and `.venv\Scripts\flask.exe` directly, or use a terminal with script activation enabled.

## Configure MySQL

1. Start MySQL 8.0 and create/use a local account with permission to create the schema and tables.
2. Run `schema.sql` in MySQL Workbench, or from Command Prompt (not PowerShell) in this folder:

   ```bat
   mysql -u root -p < schema.sql
   ```

   The script creates `ac_repair_db`, all tables, keys, indexes, constraints, and six editable demo services/prices.
3. Edit `.env` with your local MySQL host, port, username, password, and database. The values in `.env.example` are placeholders.
4. Generate a strong Flask session key:

   ```powershell
   py -3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Put the generated value in `.env` as `SECRET_KEY=...`. Do not commit `.env` or share the key. The repository ignores it. For a production HTTPS deployment, set `SESSION_COOKIE_SECURE=true` and use a production WSGI server behind HTTPS.

The default database name is `ac_repair_db`; override it with `MYSQL_DATABASE` if needed. `config.py` loads the local `.env` file and uses parameterized MySQL Connector queries throughout the app.

## Create Accounts Securely

Create the first administrator interactively after MySQL and `.env` are configured:

```powershell
flask --app app init-admin
```

The command prompts for the administrator profile and reads the password without echoing it. It refuses to create another administrator if one already exists. There are no built-in production credentials.

To create local sample accounts for workflow testing, run:

```powershell
flask --app app create-demo-accounts
```

The command creates `customer.demo@example.test` and `technician.demo@example.test` only when they do not already exist. It prompts for a distinct password for each account (12 characters minimum); passwords are never printed or stored in source files. Customer accounts can also be created through the public registration page, and administrators can create technicians in the dashboard.

## Run the Application

With the virtual environment active and `.env` configured:

```powershell
flask --app app run --debug
```

Open <http://127.0.0.1:5000>. Flask's development server is for local use only; deploy with a production WSGI server and HTTPS.

## Deploy to Vercel

`api/index.py` exposes the Flask application as a Vercel Python Function. `vercel.json` routes requests to it and bundles the Jinja templates and static assets.

Vercel cannot connect to the local MySQL instance at `127.0.0.1`. Before deployment, provision a hosted MySQL 8 database reachable from Vercel, apply `schema.sql` to it, and create a restricted database user. In the Vercel project settings, add these Production environment variables:

- `SECRET_KEY`: a strong random value, generated with the command in the MySQL setup section.
- `MYSQL_HOST`: the hosted database hostname, not `127.0.0.1` or `localhost`.
- `MYSQL_PORT`: the provider's MySQL port, usually `3306`.
- `MYSQL_USER`, `MYSQL_PASSWORD`, and `MYSQL_DATABASE`: credentials and database name for the restricted application user.
- `SESSION_COOKIE_SECURE`: `true`.

Import `thaufick18/ac-service` into Vercel with the repository root as the project root, or use the CLI from this directory:

```powershell
npx vercel login
npx vercel link
npx vercel deploy --prod
```

The CLI requires an authenticated Vercel account. This workspace has no Vercel login yet, and deployment must wait until the hosted MySQL endpoint and Vercel Production variables are configured; deploying against the local `.env` would produce database errors. Never commit `.env` or `.env*.local`.

To create the first production administrator without adding credentials to source control, pull the Production environment locally and run the interactive initializer:

```powershell
npx vercel env pull .env.production.local
.\.venv\Scripts\flask.exe --env-file .env.production.local --app app init-admin
```

The initializer reads the administrator password without echoing it. The pulled environment file is ignored by Git.

## Booking-to-Repair Test

With MySQL running and `.env` configured, run the automated live workflow test from this folder:

```powershell
python -m unittest discover -s tests -v
```

It creates uniquely named temporary accounts and requests, verifies price authority, ownership, cancellation, assignment, completion, payment, contact, and CSV reporting, then removes its test records.

1. Register a customer or sign in as `customer.demo@example.test` with the password entered by the account command.
2. Book a service. The estimate is loaded from the active catalog on the server; the request confirmation shows a unique `AC-...` service ID.
3. Open **My Requests** or **Track Service** to see the appointment and status. Tracking is restricted to requests owned by the signed-in customer. Cancel a request while its status is `Pending` to verify cancellation rules.
4. Sign in as the administrator created by `init-admin`. In **Repair requests**, search/filter for the new service ID and assign the demo technician.
5. Sign in as `technician.demo@example.test`. In the technician dashboard, progress the assigned job through **Technician On the Way**, **In Progress**, and **Completed**. Enter repair notes and the final charge when completing it.
6. Return as administrator, mark the completed request's payment as **Received**, and confirm the revenue total changes. Download the service report CSV.
7. Submit the contact form and verify the saved message appears in the administrator dashboard.

## Security and Behavior Notes

- Passwords use Werkzeug's adaptive password hashing; raw passwords are never stored.
- Forms use Flask-WTF CSRF protection; server-side validation runs independently of browser validation.
- The session identifies the account, and each protected request rechecks that the account remains active.
- Customers can view/cancel only their own eligible requests. Technicians can update only requests assigned to their technician profile and can follow only the defined status progression. Administrators manage all service records, accounts, catalog prices, and payment records.
- Customers cannot set repair status, final price, or payment state. The estimated service price is looked up from the service catalog on booking.
- Database errors are not shown to users. For deployment, configure application logging, database backups, HTTPS, and a restricted database account.
