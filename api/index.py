import json
import os
from requests import get
from jinja2 import Template
from eth_utils import event_abi_to_log_topic, function_abi_to_4byte_selector
from json import loads, dumps
from flask import Flask, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()


PORT = 3000
# should be env var, but whatever

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY")
POLYGONSCAN_API_KEY = os.environ.get("POLYGONSCAN_API_KEY")
BSCSCAN_API_KEY = os.environ.get("BSCSCAN_API_KEY")
ETHERSCAN_BASE_URL = 'https://api.etherscan.io'
POLYGONSCAN_BASE_URL = 'https://api.polygonscan.com'
BSCSCAN_BASE_URL = 'https://api.bscscan.com/'

CHAINCONFIG = {'ethereum': {'API_KEY': ETHERSCAN_API_KEY, "BASE_URL": ETHERSCAN_BASE_URL},
               'polygon': {'API_KEY': POLYGONSCAN_API_KEY, "BASE_URL": POLYGONSCAN_BASE_URL},
               'binance-smart-chain': {'API_KEY': BSCSCAN_API_KEY, "BASE_URL": BSCSCAN_BASE_URL}}


SOLIDITY_TO_BQ_TYPES = {
    'address': 'STRING',
}
SQL_TEMPLATE_FOR_EVENT = '''
CREATE TEMP FUNCTION
  PARSE_LOG(data STRING, topics ARRAY<STRING>)
  RETURNS STRUCT<{{struct_fields}}>
  LANGUAGE js AS """
    var parsedEvent = {{abi}}
    return abi.decodeEvent(parsedEvent, data, topics, false);
"""
OPTIONS
  ( library="https://storage.googleapis.com/ethlab-183014.appspot.com/ethjs-abi.js" );

WITH parsed_logs AS
(SELECT
    logs.block_timestamp AS block_timestamp
    ,logs.block_number AS block_number
    ,logs.transaction_hash AS transaction_hash
    ,logs.log_index AS log_index
    ,PARSE_LOG(logs.data, logs.topics) AS parsed
FROM `bigquery-public-data.crypto_ethereum.logs` AS logs
WHERE address = '{{contract_address}}'
  AND topics[SAFE_OFFSET(0)] = '{{selector}}'
)
SELECT
     block_timestamp
     ,block_number
     ,transaction_hash
     ,log_index{% for column in columns %}
    ,parsed.{{ column }} AS `{{ column }}`{% endfor %}
FROM parsed_logs
'''

SQL_TEMPLATE_FOR_FUNCTION = '''
CREATE TEMP FUNCTION
    PARSE_TRACE(data STRING)
    RETURNS STRUCT<{{struct_fields}}, error STRING>
    LANGUAGE js AS """
    var abi = {{abi}};
    var interface_instance = new ethers.utils.Interface([abi]);

    var result = {};
    try {
        var parsedTransaction = interface_instance.parseTransaction({data: data});
        var parsedArgs = parsedTransaction.args;

        if (parsedArgs && parsedArgs.length >= abi.inputs.length) {
            for (var i = 0; i < abi.inputs.length; i++) {
                var paramName = abi.inputs[i].name;
                var paramValue = parsedArgs[i];
                if (abi.inputs[i].type === 'address' && typeof paramValue === 'string') {
                    // For consistency all addresses are lowercase.
                    paramValue = paramValue.toLowerCase();
                }
                result[paramName] = paramValue;
            }
        } else {
            result['error'] = 'Parsed transaction args is empty or has too few values.';
        }
    } catch (e) {
        result['error'] = e.message;
    }

    return result;
"""
OPTIONS
  ( library="gs://blockchain-etl-bigquery/ethers.js" );

WITH parsed_traces AS
(SELECT
    traces.block_timestamp AS block_timestamp
    ,traces.block_number AS block_number
    ,traces.transaction_hash AS transaction_hash
    ,traces.trace_address AS trace_address
    ,PARSE_TRACE(traces.input) AS parsed
FROM `bigquery-public-data.crypto_ethereum.traces` AS traces
WHERE to_address = '{{contract_address}}'
  AND STARTS_WITH(traces.input, '{{selector}}')
  )
SELECT
     block_timestamp
     ,block_number
     ,transaction_hash
     ,trace_address
     ,parsed.error AS error
     {% for column in columns %}
    ,parsed.{{ column }} AS `{{ column }}`
    {% endfor %}
FROM parsed_traces
'''

app = Flask(__name__)
CORS(app)

dataset_name = '<INSERT_DATASET_NAME>'
table_prefix = '<TABLE_PREFIX>'
table_description = ''

# UTILS


def read_abi_from_address(address, chain):
    config = CHAINCONFIG.get(chain, {})
    a = address.lower()
    k = config.get("API_KEY")
    base_url = config.get("BASE_URL")
    url = f'{base_url}/api?module=contract&action=getabi&address={a}&apikey={k}'
    json_response = get(url).json()
    return loads(json_response['result'])


def read_contract(contract, chain):
    if contract is not None and contract.startswith('0x'):
        config = CHAINCONFIG.get(chain, {})
        a = contract.lower()
        k = config.get("API_KEY")
        base_url = config.get("BASE_URL")
        url = f'{base_url}/api?module=contract&action=getsourcecode&address={a}&apikey={k}'
        json_response = get(url).json()
        contract = [x for x in json_response['result']
                    if 'ContractName' in x][0]
        return contract
    else:
        return {
            'ContractName': 'unknown'
        }


def create_table_name(abi):
    return table_prefix + '_event_' + abi['name']


def abi_to_table_definition(abi, contract_address, parser_type):
    table_name = create_table_name(abi)
    result = {}
    result['parser'] = {
        'type': parser_type,
        'contract_address': contract_address,
        'abi': abi,
        'field_mapping': {}
    }

    def transform_params(params):
        transformed_params = []
        for param in params:
            if param.get('type') == 'tuple' and param.get('components') is not None:
                transformed_params.append({
                    'name': param.get('name'),
                    'description': '',
                    'type': 'RECORD',
                    'fields': transform_params(param.get('components'))
                })
            else:
                transformed_params.append({
                    'name': param.get('name'),
                    'description': '',
                    'type': 'STRING'  # we sometimes get parsing errors, so safest to make all STRING
                })
        return transformed_params

    result['table'] = {
        'dataset_name': dataset_name,
        'table_name': table_name,
        'table_description': table_description,
        'schema': transform_params(abi['inputs'])
    }
    return result


def contract_to_table_definitions(contract, chain):
    if contract is not None and contract.startswith('0x'):
        contract_address = contract.lower()
        abi = read_abi_from_address(contract, chain)
    else:
        contract_address = 'unknown'
        abi = json.loads(contract)

    result = {}
    for a in filter_by_type(abi, 'event'):
        result[a['name']] = abi_to_table_definition(a, contract_address, 'log')
    for a in filter_by_type(abi, 'function'):
        result[a['name']] = abi_to_table_definition(
            a, contract_address, 'trace')
    return result


def s2bq_type(type):
    return SOLIDITY_TO_BQ_TYPES.get(type, 'STRING')


def filter_by_type(abi, type):
    for a in abi:
        if a['type'] == type:
            yield a


def get_columns_from_event_abi(event_abi):
    return [a.get('name') for a in event_abi['inputs']]


def create_struct_fields_from_event_abi(event_abi):
    return ', '.join(['`' + a.get('name') + '` ' + s2bq_type(a.get('type')) for a in event_abi['inputs']])


def abi_to_sql(abi, template, contract_address):
    if abi['type'] == 'event':
        selector = '0x' + event_abi_to_log_topic(abi).hex()
    else:
        selector = '0x' + function_abi_to_4byte_selector(abi).hex()

    struct_fields = create_struct_fields_from_event_abi(abi)
    columns = get_columns_from_event_abi(abi)
    return template.render(
        abi=dumps(abi),
        contract_address=contract_address.lower(),
        selector=selector,
        struct_fields=struct_fields,
        columns=columns
    )


def contract_to_sqls(contract, chain):
    if contract is not None and contract.startswith('0x'):
        contract_address = contract.lower()
        abi = read_abi_from_address(contract_address, chain)
    else:
        contract_address = 'unknown'
        abi = json.loads(contract)

    event_tpl = Template(SQL_TEMPLATE_FOR_EVENT)
    function_tpl = Template(SQL_TEMPLATE_FOR_FUNCTION)

    result = {}
    for a in filter_by_type(abi, 'event'):
        result[a['name']] = abi_to_sql(a, event_tpl, contract_address)
    for a in filter_by_type(abi, 'function'):
        result[a['name']] = abi_to_sql(a, function_tpl, contract_address)
    return result

# WEB SERVER


@app.route('/api/')
def index():
    return jsonify({'status': 'alive'})


@app.route('/api/test')
def test():
    return jsonify({'status': 'test'})


@app.route('/api/queries/<contract>/<chain>')
def queries(contract, chain):
    queries = contract_to_sqls(contract, chain)
    return jsonify(queries)


@app.route('/api/tables/<contract>/<chain>')
def tables(contract, chain):
    tables = contract_to_table_definitions(contract, chain)
    return jsonify(tables)


@app.route('/api/contract/<contract>/<chain>')
def contract(contract, chain):
    c = read_contract(contract, chain)
    return jsonify(c)


if __name__ == "__main__":
    app.run(debug=True, host='127.0.0.1', port=PORT)
