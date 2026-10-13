CREATE DATABASE IF NOT EXISTS ac_repair_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE ac_repair_db;

CREATE TABLE IF NOT EXISTS users (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    email VARCHAR(254) NOT NULL UNIQUE,
    phone VARCHAR(30) NOT NULL,
    address VARCHAR(500) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('customer', 'technician', 'admin') NOT NULL DEFAULT 'customer',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_users_role_active (role, active)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS technicians (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT UNSIGNED NOT NULL UNIQUE,
    specialization VARCHAR(160) NOT NULL DEFAULT 'Residential air conditioning',
    availability ENUM('available', 'busy', 'unavailable') NOT NULL DEFAULT 'available',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_technicians_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS services (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    service_name VARCHAR(100) NOT NULL UNIQUE,
    description VARCHAR(500) NOT NULL,
    estimated_price DECIMAL(10,2) UNSIGNED NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT chk_service_price CHECK (estimated_price >= 0)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS service_requests (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    request_code VARCHAR(24) NOT NULL UNIQUE,
    customer_id BIGINT UNSIGNED NOT NULL,
    technician_id BIGINT UNSIGNED NULL,
    service_id BIGINT UNSIGNED NOT NULL,
    ac_type ENUM('Split AC', 'Window AC', 'Central AC') NOT NULL,
    ac_brand VARCHAR(100) NOT NULL,
    problem_description TEXT NOT NULL,
    service_address VARCHAR(500) NOT NULL,
    preferred_date DATE NOT NULL,
    preferred_time TIME NOT NULL,
    estimated_price DECIMAL(10,2) UNSIGNED NOT NULL,
    final_price DECIMAL(10,2) UNSIGNED NULL,
    status ENUM('Pending', 'Assigned', 'Technician On the Way', 'In Progress', 'Completed', 'Cancelled') NOT NULL DEFAULT 'Pending',
    repair_notes TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_requests_customer FOREIGN KEY (customer_id) REFERENCES users(id),
    CONSTRAINT fk_requests_technician FOREIGN KEY (technician_id) REFERENCES technicians(id) ON DELETE SET NULL,
    CONSTRAINT fk_requests_service FOREIGN KEY (service_id) REFERENCES services(id),
    CONSTRAINT chk_request_estimated_price CHECK (estimated_price >= 0),
    CONSTRAINT chk_request_final_price CHECK (final_price IS NULL OR final_price >= 0),
    INDEX idx_requests_customer_created (customer_id, created_at),
    INDEX idx_requests_technician_status (technician_id, status),
    INDEX idx_requests_status_created (status, created_at),
    INDEX idx_requests_preferred_date (preferred_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS payments (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    service_request_id BIGINT UNSIGNED NOT NULL UNIQUE,
    amount DECIMAL(10,2) UNSIGNED NOT NULL,
    status ENUM('Pending', 'Received', 'Refunded') NOT NULL DEFAULT 'Pending',
    recorded_at TIMESTAMP NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_payments_request FOREIGN KEY (service_request_id) REFERENCES service_requests(id) ON DELETE CASCADE,
    CONSTRAINT chk_payment_amount CHECK (amount >= 0),
    INDEX idx_payments_status_recorded (status, recorded_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS contact_messages (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    email VARCHAR(254) NOT NULL,
    phone VARCHAR(30) NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_messages_created (created_at)
) ENGINE=InnoDB;

INSERT INTO services (service_name, description, estimated_price, active) VALUES
    ('Installation', 'Professional installation and system commissioning.', 129.00, TRUE),
    ('General Service', 'Seasonal inspection, cleaning and performance check.', 79.00, TRUE),
    ('Gas Refilling', 'Leak assessment and refrigerant top-up.', 119.00, TRUE),
    ('Cooling Issue', 'Diagnosis and repair for poor or inconsistent cooling.', 89.00, TRUE),
    ('Water Leakage', 'Drain line and condensate leak diagnosis.', 69.00, TRUE),
    ('Electrical Repair', 'Electrical fault finding and component repair.', 99.00, TRUE)
ON DUPLICATE KEY UPDATE description = VALUES(description);
