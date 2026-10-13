import os
import re
import unittest
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import mysql.connector
from werkzeug.security import generate_password_hash

os.environ.setdefault("SECRET_KEY", "local-integration-test-only")

import app as application
from config import Config


class BookingWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tag = uuid4().hex
        self.customer_email = f"customer-{self.tag}@example.test"
        self.other_email = f"other-{self.tag}@example.test"
        self.admin_email = f"admin-{self.tag}@example.test"
        self.technician_email = f"technician-{self.tag}@example.test"
        self.password = "local-workflow-test-password"
        self.app = application.app
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.create_staff_accounts()
        self.addCleanup(self.cleanup_test_records)

    def connect(self):
        return mysql.connector.connect(**Config.MYSQL_CONFIG)

    def create_staff_accounts(self):
        connection = self.connect()
        cursor = connection.cursor()
        password_hash = generate_password_hash(self.password)
        for name, email, role in (
            ("Workflow Admin", self.admin_email, "admin"),
            ("Workflow Technician", self.technician_email, "technician"),
        ):
            cursor.execute(
                """INSERT INTO users (name, email, phone, address, password_hash, role)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (name, email, "+1 555 010 2222", "200 Test Avenue", password_hash, role),
            )
            user_id = cursor.lastrowid
            if role == "technician":
                cursor.execute(
                    "INSERT INTO technicians (user_id, specialization) VALUES (%s, %s)",
                    (user_id, "Residential AC repair"),
                )
        connection.commit()
        cursor.close()
        connection.close()

    def create_customer(self, email, name):
        client = self.app.test_client()
        response = client.post(
            "/register",
            data={
                "name": name,
                "email": email,
                "phone": "+1 555 010 1111",
                "address": "100 Test Avenue",
                "password": self.password,
            },
        )
        self.assertEqual(response.status_code, 302)
        response = client.post("/login", data={"email": email, "password": self.password})
        self.assertEqual(response.status_code, 302)
        return client

    def booking_data(self, service_id):
        return {
            "service_id": str(service_id),
            "ac_type": "Split AC",
            "ac_brand": "TestCool",
            "service_address": "100 Test Avenue, Test City",
            "preferred_date": (date.today() + timedelta(days=1)).isoformat(),
            "preferred_time": "10:00",
            "problem_description": "System is not cooling consistently.",
            "estimated_price": "0.01",
        }

    def test_booking_to_completion_and_payment(self):
        customer = self.create_customer(self.customer_email, "Workflow Customer")
        connection = self.connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, estimated_price FROM services WHERE active = 1 ORDER BY id LIMIT 1")
        service = cursor.fetchone()
        service_id = service["id"]
        cursor.close()
        connection.close()

        response = customer.post("/book", data=self.booking_data(service_id))
        self.assertEqual(response.status_code, 200)
        match = re.search(r"AC-[A-F0-9]{10}", response.get_data(as_text=True))
        self.assertIsNotNone(match)
        request_code = match.group(0)

        connection = self.connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, status, estimated_price FROM service_requests WHERE request_code = %s", (request_code,))
        service_request = cursor.fetchone()
        self.assertEqual(service_request["status"], "Pending")
        self.assertEqual(service_request["estimated_price"], service["estimated_price"])
        cursor.execute("SELECT id FROM users WHERE email = %s", (self.admin_email,))
        admin_id = cursor.fetchone()["id"]
        cursor.execute("SELECT id, user_id FROM technicians WHERE user_id = (SELECT id FROM users WHERE email = %s)", (self.technician_email,))
        technician = cursor.fetchone()
        connection.close()

        admin = self.app.test_client()
        with admin.session_transaction() as session:
            session["user_id"] = admin_id
        response = admin.post(
            "/admin/requests",
            data={
                "request_id": service_request["id"],
                "action": "assign",
                "technician_id": technician["id"],
            },
        )
        self.assertEqual(response.status_code, 302)

        technician_client = self.app.test_client()
        with technician_client.session_transaction() as session:
            session["user_id"] = technician["user_id"]
        response = technician_client.get("/technician")
        self.assertEqual(response.status_code, 200)
        for status, extra in (
            ("Technician On the Way", {}),
            ("In Progress", {}),
            ("Completed", {"final_price": "123.45"}),
        ):
            response = technician_client.post(
                f"/technician/jobs/{service_request['id']}/update",
                data={"status": status, "repair_notes": "Workflow test repair", **extra},
            )
            self.assertEqual(response.status_code, 302)

        response = admin.post(
            "/admin/requests",
            data={
                "request_id": service_request["id"],
                "action": "payment",
                "payment_status": "Received",
            },
        )
        self.assertEqual(response.status_code, 302)

        customer.get(f"/track?request_code={request_code}")
        response = customer.get(f"/track?request_code={request_code}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Completed", response.data)
        self.assertIn(b"Workflow Technician", response.data)

        other_customer = self.create_customer(self.other_email, "Other Customer")
        response = other_customer.get(f"/track?request_code={request_code}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Workflow Technician", response.data)
        self.assertIn(b"No service request was found", response.data)

        response = admin.get("/admin/report.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn(request_code.encode(), response.data)

        response = customer.post(
            "/contact",
            data={
                "name": "Workflow Customer",
                "email": self.customer_email,
                "phone": "+1 555 010 1111",
                "message": "Workflow test contact message.",
            },
        )
        self.assertEqual(response.status_code, 302)

        connection = self.connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """SELECT r.status, r.final_price, p.status AS payment_status, p.amount
               FROM service_requests r JOIN payments p ON p.service_request_id = r.id
               WHERE r.id = %s""",
            (service_request["id"],),
        )
        result = cursor.fetchone()
        self.assertEqual(result["status"], "Completed")
        self.assertEqual(result["final_price"], Decimal("123.45"))
        self.assertEqual(result["payment_status"], "Received")
        self.assertEqual(result["amount"], Decimal("123.45"))
        cursor.close()
        connection.close()

        cancel_response = customer.post("/book", data=self.booking_data(service_id))
        cancel_match = re.search(r"AC-[A-F0-9]{10}", cancel_response.get_data(as_text=True))
        self.assertIsNotNone(cancel_match)
        connection = self.connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id FROM service_requests WHERE request_code = %s", (cancel_match.group(0),))
        pending_id = cursor.fetchone()["id"]
        cursor.close()
        connection.close()
        response = customer.post(f"/requests/{pending_id}/cancel")
        self.assertEqual(response.status_code, 302)
        connection = self.connect()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT status FROM service_requests WHERE id = %s", (pending_id,))
        self.assertEqual(cursor.fetchone()["status"], "Cancelled")
        cursor.close()
        connection.close()

    def cleanup_test_records(self):
        connection = self.connect()
        cursor = connection.cursor()
        emails = (self.customer_email, self.other_email, self.admin_email, self.technician_email)
        placeholders = ", ".join(["%s"] * len(emails))
        cursor.execute(f"SELECT id FROM users WHERE email IN ({placeholders})", emails)
        user_ids = [row[0] for row in cursor.fetchall()]
        if user_ids:
            user_marks = ", ".join(["%s"] * len(user_ids))
            cursor.execute(f"SELECT id FROM technicians WHERE user_id IN ({user_marks})", tuple(user_ids))
            technician_ids = [row[0] for row in cursor.fetchall()]
            if technician_ids:
                tech_marks = ", ".join(["%s"] * len(technician_ids))
                cursor.execute(f"DELETE FROM service_requests WHERE technician_id IN ({tech_marks})", tuple(technician_ids))
            cursor.execute(f"DELETE FROM service_requests WHERE customer_id IN ({user_marks})", tuple(user_ids))
            cursor.execute(f"DELETE FROM users WHERE id IN ({user_marks})", tuple(user_ids))
        cursor.execute("DELETE FROM contact_messages WHERE email IN (%s, %s)", (self.customer_email, self.other_email))
        connection.commit()
        cursor.close()
        connection.close()


if __name__ == "__main__":
    unittest.main()
