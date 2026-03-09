# 🏠 Local Setup Guide: Universal Deployer

Follow these steps to run the Universal Deployer on your local machine (or a friend's laptop).

## 1. Prerequisites
Make sure you have the following installed:
- **Python 3.9+** ([Download](https://www.python.org/downloads/))
- **Node.js 18+** ([Download](https://nodejs.org/)) — *Required for building frontend/Node apps*
- **Git** ([Download](https://git-scm.com/)) — *Required for cloning repositories*

## 2. Clone the Repository
Open a terminal and run:
```bash
git clone https://github.com/Vishwas28789/aws
cd aws
```

## 3. Install Python Dependencies
Create a virtual environment (optional but recommended) and install `boto3`:
```bash
python -m venv venv
# On Windows:
venv\Scripts\activate
# On Mac/Linux:
source venv/bin/activate

pip install -r requirements.txt
```

## 4. Run the Application
Start the local server using the root `main.py`:
```bash
python main.py
```

## 5. Access the Dashboard
Open your browser and go to:
**[http://localhost:8000](http://localhost:8000)**

---

## 🔑 AWS Credentials
When you click **Analyze** and then **Deploy**, you will be asked for:
1. **AWS Access Key ID**
2. **AWS Secret Access Key**
3. **AWS Region** (e.g., `us-east-1`)

These are used *only* for that session to interact with your AWS account.

## 🚀 Troubleshooting
- **Node.js Error**: Ensure `node --version` works in your terminal. We need it to build React/Node projects.
- **Port Conflict**: If port 8000 is busy, run `python main.py --port 8080`.
- **API Offline**: Refresh the page to ensure the frontend connects to the backend.
