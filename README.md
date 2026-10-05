# TP Masuhla

TP Masuhla is a full-stack e-commerce web application for a cleaning company. Customers can book cleaning services for their home or office, and shop for cleaning products in an online store, paying securely through Stripe.

## Features

### Accounts and Checkout
- Create an account and log in to an existing one
- Guest checkout: continue without signing in, and your details are collected at checkout

### Services
- Schedule a cleaning service for a **home** or an **office**
- Choose the service type, date, and time
- Provide the address and any special instructions

### Products
- Browse an online store of cleaning products
- Add products to the cart, update quantities, or remove items
- Check out securely with Stripe

## Tech Stack

| Layer          | Technology                          |
|----------------|-------------------------------------|
| Frontend       | HTML, CSS                           |
| Backend        | Python, Flask                       |
| Database       | PostgreSQL                          |
| Payments       | Stripe                              |
| Image storage  | Cloudflare                          |


## Getting Started

### Prerequisites
- Python 3.10 or higher
- PostgreSQL
- A Stripe account (for API keys)
- A Cloudflare account (for image storage)

### Installation

1. **Clone the repository**
```bash
   git clone [your-repo-url]
   cd tp-masuhla
```

2. **Create and activate a virtual environment**
```bash
   python -m venv venv

   # Windows
   venv\Scripts\activate

   # macOS / Linux
   source venv/bin/activate
```

3. **Install dependencies**
```bash
   pip install -r requirements.txt
```

4. **Set up the database**

   Create a PostgreSQL database:
```sql
   CREATE DATABASE tp_masuhla;
```

5. **Configure environment variables**

   Create a `.env` file in the project root:
```env
   SECRET_KEY=your_flask_secret_key
   DATABASE_URL=postgresql://username:password@localhost:5432/tp_masuhla


   STRIPE_PUBLIC_KEY=your_stripe_publishable_key
   STRIPE_SECRET_KEY=your_stripe_secret_key
   STRIPE_WEBHOOK_SECRET=your_stripe_webhook_secret
   CLOUDFLARE_ACCOUNT_ID=your_cloudflare_account_id
   CLOUDFLARE_API_TOKEN=your_cloudflare_api_token
```

6. **Run database migrations / create tables**
```bash
   [your command, e.g. flask db upgrade]
```
                              
7. **Start the application**
```bash
   flask server.py
```
   The app will be available at `http://localhost:5000`.

## Payments (Stripe)

Product checkout is handled by Stripe. To test payments locally:

- Use your Stripe **test** API keys
- Use Stripe's test card number `4242 4242 4242 4242` with any future expiry date and any CVC

## Image Uploads (Cloudflare)

Product images are uploaded to and served from Cloudflare. Make sure your Cloudflare credentials are set in the `.env` file before uploading images.

## Usage

1. Open the site and choose **Services** or **Products**.
2. **Services:** pick home or office cleaning, select a date and time, and submit your booking.
3. **Products:** add items to your cart and proceed to checkout.
4. Log in or create an account to save your details, or continue as a guest and enter your details at checkout.
5. Complete payment through Stripe.

## Screenshots

_Add screenshots of the home page, services page, product store, and checkout here._

## Future Improvements

- [Order history for logged-in users]
- [Email confirmations for bookings and orders]
- [Admin dashboard to manage services, products, and orders]

## Author

**Tshepo Shomang**

## License

[Add a license, e.g. MIT]
