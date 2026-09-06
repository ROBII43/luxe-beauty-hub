CREATE DATABASE IF NOT EXISTS luxe CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE luxe;

CREATE TABLE settings (
  setting_key VARCHAR(100) PRIMARY KEY,
  setting_value JSON NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
CREATE TABLE roles (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(40) UNIQUE NOT NULL);
CREATE TABLE users (
  id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(160) NOT NULL, email VARCHAR(180) UNIQUE NOT NULL,
  phone VARCHAR(40), password_hash VARCHAR(255) NOT NULL, role_id INT, active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (role_id) REFERENCES roles(id)
);
CREATE TABLE categories (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(120) NOT NULL UNIQUE,
  description VARCHAR(255),
  image_url TEXT,
  active BOOLEAN DEFAULT TRUE,
  sort_order INT DEFAULT 0
);
CREATE TABLE subcategories (
  id INT AUTO_INCREMENT PRIMARY KEY, category_id INT NOT NULL, name VARCHAR(120) NOT NULL,
  active BOOLEAN DEFAULT TRUE, sort_order INT DEFAULT 0, UNIQUE KEY category_subcategory (category_id, name),
  FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
);
CREATE TABLE brands (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(120) UNIQUE NOT NULL, active BOOLEAN DEFAULT TRUE);
CREATE TABLE products (
  id INT AUTO_INCREMENT PRIMARY KEY,
  sku VARCHAR(80) NOT NULL UNIQUE,
  name VARCHAR(180) NOT NULL,
  brand VARCHAR(120),
  category_id INT,
  description TEXT,
  price DECIMAL(12,2) NOT NULL,
  discount_price DECIMAL(12,2),
  stock INT NOT NULL DEFAULT 0,
  minimum_stock INT NOT NULL DEFAULT 5,
  rating DECIMAL(2,1) DEFAULT 0,
  image_url TEXT,
  tags JSON,
  featured BOOLEAN DEFAULT FALSE,
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  FOREIGN KEY (category_id) REFERENCES categories(id),
  INDEX idx_products_category (category_id), INDEX idx_products_stock (stock), INDEX idx_products_active (active)
);
CREATE TABLE product_images (id INT AUTO_INCREMENT PRIMARY KEY, product_id INT NOT NULL, image_url TEXT NOT NULL, sort_order INT DEFAULT 0, is_primary BOOLEAN DEFAULT FALSE, FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE);
CREATE TABLE product_variants (id INT AUTO_INCREMENT PRIMARY KEY, product_id INT NOT NULL, name VARCHAR(120), sku VARCHAR(80) UNIQUE, price DECIMAL(12,2), stock INT DEFAULT 0, FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE);
CREATE TABLE inventory (product_id INT PRIMARY KEY, stock INT NOT NULL DEFAULT 0, minimum_stock INT NOT NULL DEFAULT 5, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE);
CREATE TABLE customers (
  id INT AUTO_INCREMENT PRIMARY KEY,
  full_name VARCHAR(160) NOT NULL, email VARCHAR(180) UNIQUE NOT NULL,
  phone VARCHAR(40), password_hash VARCHAR(255), active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE permissions (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(80) UNIQUE NOT NULL);
CREATE TABLE role_permissions (role_id INT NOT NULL, permission_id INT NOT NULL, PRIMARY KEY(role_id, permission_id), FOREIGN KEY(role_id) REFERENCES roles(id) ON DELETE CASCADE, FOREIGN KEY(permission_id) REFERENCES permissions(id) ON DELETE CASCADE);
CREATE TABLE carts (id INT AUTO_INCREMENT PRIMARY KEY, customer_id INT, session_token VARCHAR(180) UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(customer_id) REFERENCES customers(id));
CREATE TABLE cart_items (cart_id INT NOT NULL, product_id INT NOT NULL, quantity INT NOT NULL, PRIMARY KEY(cart_id, product_id), FOREIGN KEY(cart_id) REFERENCES carts(id) ON DELETE CASCADE, FOREIGN KEY(product_id) REFERENCES products(id));
CREATE TABLE wishlists (id INT AUTO_INCREMENT PRIMARY KEY, customer_id INT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(customer_id) REFERENCES customers(id));
CREATE TABLE wishlist_items (wishlist_id INT NOT NULL, product_id INT NOT NULL, PRIMARY KEY(wishlist_id, product_id), FOREIGN KEY(wishlist_id) REFERENCES wishlists(id) ON DELETE CASCADE, FOREIGN KEY(product_id) REFERENCES products(id));
CREATE TABLE orders (
  id INT AUTO_INCREMENT PRIMARY KEY,
  order_number VARCHAR(40) UNIQUE NOT NULL, customer_id INT,
  status ENUM('Pending','Confirmed','Processing','Ready for Delivery','Shipped','Delivered','Cancelled','Returned') DEFAULT 'Pending',
  payment_method VARCHAR(60), payment_status VARCHAR(40) DEFAULT 'Pending',
  subtotal DECIMAL(12,2) NOT NULL, delivery_fee DECIMAL(12,2) DEFAULT 0, total DECIMAL(12,2) NOT NULL,
  delivery_address TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (customer_id) REFERENCES customers(id), INDEX idx_orders_status(status), INDEX idx_orders_created(created_at)
);
CREATE TABLE order_items (
  id INT AUTO_INCREMENT PRIMARY KEY, order_id INT NOT NULL, product_id INT NOT NULL,
  quantity INT NOT NULL, unit_price DECIMAL(12,2) NOT NULL,
  FOREIGN KEY (order_id) REFERENCES orders(id), FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE TABLE payments (id INT AUTO_INCREMENT PRIMARY KEY, order_id INT NOT NULL, method VARCHAR(60), status VARCHAR(40), provider_reference VARCHAR(180), amount DECIMAL(12,2), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(order_id) REFERENCES orders(id));
CREATE TABLE deliveries (id INT AUTO_INCREMENT PRIMARY KEY, order_id INT NOT NULL, address TEXT, county VARCHAR(100), town VARCHAR(100), instructions TEXT, status VARCHAR(40), FOREIGN KEY(order_id) REFERENCES orders(id));
CREATE TABLE inventory_movements (
  id INT AUTO_INCREMENT PRIMARY KEY, product_id INT NOT NULL, quantity_change INT NOT NULL,
  previous_stock INT NOT NULL, new_stock INT NOT NULL, reason VARCHAR(180), user_name VARCHAR(120),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE TABLE audit_logs (
  id INT AUTO_INCREMENT PRIMARY KEY, user_name VARCHAR(120), action VARCHAR(120), description TEXT,
  ip_address VARCHAR(45), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE reviews (
  id INT AUTO_INCREMENT PRIMARY KEY, product_id INT NOT NULL, customer_id INT NOT NULL,
  rating TINYINT NOT NULL, review_text TEXT NOT NULL, status ENUM('Pending','Approved','Hidden') DEFAULT 'Pending',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (product_id) REFERENCES products(id), FOREIGN KEY (customer_id) REFERENCES customers(id),
  UNIQUE KEY one_review_per_customer (product_id, customer_id)
);
CREATE TABLE coupons (
  id INT AUTO_INCREMENT PRIMARY KEY, code VARCHAR(50) UNIQUE NOT NULL,
  discount_type ENUM('percentage','fixed') NOT NULL, value DECIMAL(12,2) NOT NULL,
  starts_at DATETIME, ends_at DATETIME, usage_limit INT, used_count INT DEFAULT 0,
  minimum_order DECIMAL(12,2) DEFAULT 0, active BOOLEAN DEFAULT TRUE
);
CREATE TABLE promotions (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(150) NOT NULL, promotion_type ENUM('percentage','fixed','flash_sale') NOT NULL, value DECIMAL(12,2) NOT NULL, starts_at DATETIME, ends_at DATETIME, active BOOLEAN DEFAULT TRUE);
CREATE TABLE banners (id INT AUTO_INCREMENT PRIMARY KEY, image_url TEXT NOT NULL, heading VARCHAR(180), description TEXT, button_text VARCHAR(80), button_link TEXT, starts_at DATETIME, ends_at DATETIME, active BOOLEAN DEFAULT TRUE);
CREATE TABLE notifications (id INT AUTO_INCREMENT PRIMARY KEY, customer_id INT, type VARCHAR(80), subject VARCHAR(180), body TEXT, sent_at DATETIME, FOREIGN KEY(customer_id) REFERENCES customers(id));
CREATE TABLE order_status_history (
  id INT AUTO_INCREMENT PRIMARY KEY, order_id INT NOT NULL, old_status VARCHAR(40), new_status VARCHAR(40) NOT NULL,
  changed_by VARCHAR(120), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (order_id) REFERENCES orders(id)
);
