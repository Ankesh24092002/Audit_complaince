import os
import logging
import uuid
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv
from azure.cosmos import CosmosClient, PartitionKey
from openai import AzureOpenAI

# Load environment variables
load_dotenv()

# Validate and fetch environment variables
azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
api_key = os.getenv("AZURE_OPENAI_KEY")
deployment_name = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
cosmos_db_endpoint = os.getenv("COSMOS_DB_ENDPOINT")
cosmos_db_key = os.getenv("COSMOS_DB_KEY")

if not azure_endpoint or not api_key or not deployment_name or not cosmos_db_endpoint or not cosmos_db_key:
    raise ValueError("Environment variables for Azure OpenAI and Cosmos DB must be set.")

# Set up Azure OpenAI client
client = AzureOpenAI(
    azure_endpoint=azure_endpoint,
    api_key=api_key,
    api_version="2024-02-15-preview"
)

# Set up logging
logging.basicConfig(level=logging.INFO)

# Set up Cosmos DB client
cosmos_client = CosmosClient(cosmos_db_endpoint, cosmos_db_key)
database_name = 'AuditComplianceDB'
transaction_container_name = 'Transactions'
rule_container_name = 'ComplianceRules'
database = cosmos_client.create_database_if_not_exists(id=database_name)

transaction_container = database.create_container_if_not_exists(
    id=transaction_container_name,
    partition_key=PartitionKey(path="/id"),
    offer_throughput=400
)

rule_container = database.create_container_if_not_exists(
    id=rule_container_name,
    partition_key=PartitionKey(path="/id"),
    offer_throughput=400
)

# Create Flask app
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

def perform_query_chat(message_history):
    response = client.chat.completions.create(
        model="gpt35turbo16k",
        messages=message_history,
        temperature=0.7,
        max_tokens=10000,
        top_p=0.95,
        frequency_penalty=0,
        presence_penalty=0,
        stop=None
    )
    return response

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/audit', methods=['POST'])
def audit_transaction():
    data = request.json
    try:
        transaction = {
            'id': str(uuid.uuid4()),
            'date': data['date'],
            'description': data['description'],
            'amount': float(data['amount']),  # Convert to float
            'is_compliant': True
        }
    except ValueError as e:
        logging.error(f"Error converting amount to float: {e}")
        return jsonify({"error": "Invalid amount format"}), 400

    compliance_issues = check_compliance(transaction)
    transaction['is_compliant'] = len(compliance_issues) == 0
    transaction['compliance_issues'] = compliance_issues

    try:
        transaction_container.upsert_item(transaction)
        return jsonify({
            'transaction_id': transaction['id'],
            'compliance_issues': compliance_issues,
            'is_compliant': transaction['is_compliant']
        }), 201
    except Exception as e:
        logging.error(f"Error submitting data: {e}")
        return jsonify({"error": str(e)}), 500

def check_compliance(transaction):
    # Ensure that the amount is treated as a float for comparison
    try:
        transaction['amount'] = float(transaction['amount'])
    except ValueError as e:
        logging.error(f"Error converting amount to float: {e}")
        return ["Invalid transaction amount format"]

    rules = list(rule_container.read_all_items())
    compliance_issues = []

    for rule in rules:
        logging.info(f"Checking rule: {rule['rule_description']}")
        try:
            if not eval(rule['rule_check'].format(transaction=transaction)):
                logging.info(f"Rule violation: {rule['rule_description']}")
                compliance_issues.append(rule['rule_description'])
        except Exception as e:
            logging.error(f"Error evaluating rule: {rule['rule_description']}, Error: {e}")

    return compliance_issues

@app.route('/generate_report', methods=['GET'])
def generate_report():
    transactions = list(transaction_container.read_all_items())
    non_compliant_transactions = [t for t in transactions if not t['is_compliant']]

    if not non_compliant_transactions:
        return jsonify({'report': 'No non-compliant transactions found.'})

    # Prepare a detailed prompt
    detailed_transactions = [
        f"Transaction ID: {t['id']}, Date: {t['date']}, Description: {t['description']}, Amount: {t['amount']}, Issues: {t['compliance_issues']}"
        for t in non_compliant_transactions
    ]
    detailed_prompt = (
    "Generate an audit report for the following non-compliant transactions:\n" + 
    "\n".join(detailed_transactions) + 
    "\nObjective: Streamline audit and compliance.\n"
    "Challenge: Automate transaction auditing, detect non-compliance, generate reports with minimal human intervention, and update compliance requirements.\n"
    "Structure:\n"
    "1. Introduction: Overview of the audit objective and challenges.\n"
    "2. Summary: Summary of non-compliant transactions.\n"
    "3. Details: For each non-compliant transaction, provide:\n"
    "   - Transaction ID\n"
    "   - Transaction Details\n"
    "   - Issues\n"
    )

    message_history = [{"role": "user", "content": detailed_prompt}]

    response = perform_query_chat(message_history)

    # Extract the content from the response correctly
    assistant_message = response.choices[0].message.content

    return jsonify({'report': assistant_message})

@app.route('/update_compliance', methods=['POST'])
def update_compliance():
    data = request.json
    new_rule = {
        'id': str(uuid.uuid4()),
        'rule_description': data['rule_description'],
        'rule_check': data['rule_check']
    }

    try:
        rule_container.upsert_item(new_rule)
        return jsonify({'status': 'success', 'rule_id': new_rule['id']}), 201
    except Exception as e:
        logging.error(f"Error updating compliance: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=False)  # Debug mode should be False in production
