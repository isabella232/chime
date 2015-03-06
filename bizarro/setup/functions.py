from __future__ import print_function

from os import environ
from getpass import getpass
from re import match
from urllib import urlencode
from urlparse import urljoin
from datetime import datetime
from time import sleep
import json

from boto.ec2 import EC2Connection
from boto.route53 import Route53Connection
from oauth2client.client import OAuth2WebServerFlow
from itsdangerous import Signer
import gspread, requests

GITHUB_API_BASE = 'https://api.github.com/'

def check_status(resp, task):
    ''' Raise a RuntimeError if response is not HTTP 2XX.
    '''
    if resp.status_code not in range(200, 299):
        raise RuntimeError('Got {} trying to {}'.format(resp.status_code, task))

def get_input():
    '''
    '''
    github_client_id = environ['GITHUB_CLIENT_ID']
    github_client_secret = environ['GITHUB_CLIENT_SECRET']
    
    gdocs_client_id = environ['GDOCS_CLIENT_ID']
    gdocs_client_secret = environ['GDOCS_CLIENT_SECRET']

    print('--> Enter Github details:')
    username = raw_input('    Github username: ')
    password = getpass('    Github password: ')
    reponame = raw_input('    New Github repository name: ')

    if not match(r'\w+(-\w+)*$', reponame):
        raise RuntimeError('Repository "{}" does not match "\w+(-\w+)*$"'.format(reponame))

    ec2 = EC2Connection()
    route53 = Route53Connection()
    
    return github_client_id, github_client_secret, \
           gdocs_client_id, gdocs_client_secret, \
           username, password, reponame, ec2, route53

def authenticate_google(gdocs_client_id, gdocs_client_secret):
    '''
    '''
    scopes = [
        'https://spreadsheets.google.com/feeds/',

        # http://stackoverflow.com/questions/24293523/im-trying-to-access-google-drive-through-the-cli-but-keep-getting-not-authori
        'https://docs.google.com/feeds',
        ]

    flow = OAuth2WebServerFlow(gdocs_client_id, gdocs_client_secret, scopes)
    flow_info = flow.step1_get_device_and_user_codes()

    user_code, verification_url = flow_info.user_code, flow_info.verification_url
    print('--> Authenticate with Google Docs:')
    print('    Visit {verification_url} with code "{user_code}"'.format(**locals()))
    print('    (then come back here and press enter)')

    raw_input()
    credentials = flow.step2_exchange(device_flow_info=flow_info)
    
    print('--> Google Docs authentication OK')
    return credentials

def create_google_spreadsheet(credentials, reponame):
    '''
    '''
    email = 'frances@codeforamerica.org'
    gdocs_api_base = 'https://www.googleapis.com/drive/v2/files/'
    headers = {'Content-Type': 'application/json'}

    source_id = '12jUfaRBd-CU1_6BGeLFG1_qoi7Fw_vRC_SXv36eDzM0'
    url = urljoin(gdocs_api_base, '{source_id}/copy'.format(**locals()))

    gc = gspread.authorize(credentials)
    resp = gc.session.post(url, '{ }', headers=headers)
    info = json.load(resp)
    new_id = info['id']

    print('    Created spreadsheet "{title}"'.format(**info))

    url = urljoin(gdocs_api_base, new_id)
    new_title = 'Chime CMS logins for {reponame}'.format(**locals())
    patch = dict(title=new_title)
    
    gc = gspread.authorize(credentials)
    gc.session.request('PATCH', url, json.dumps(patch), headers=headers)

    print('    Updated title to "{new_title}"'.format(**locals()))

    url = urljoin(gdocs_api_base, '{new_id}/permissions'.format(**locals()))
    permission = dict(type='anyone', role='reader', withLink=True)

    gc = gspread.authorize(credentials)
    gc.session.post(url, json.dumps(permission), headers=headers)

    print('    Allowed anyone with the link to see "{new_title}"'.format(**locals()))

    query = urlencode(dict(sendNotificationEmails='true', emailMessage='Yo.'))
    url = urljoin(gdocs_api_base, '{new_id}/permissions?{query}'.format(**locals()))
    permission = dict(type='user', role='writer', emailAddress=email, value=email)

    gc = gspread.authorize(credentials)
    gc.session.post(url, json.dumps(permission), headers=headers)

    print('    Invited {email} to "{new_title}"'.format(**locals()))

    return new_id

def get_public_deploy_key(instance_dns_name, secret, salt):
    ''' Wait for and retrieve instance public key.
    '''
    signer = Signer(secret, salt)
    path = '/.well-known/deploy-key.txt'
    
    while True:
        print('    Waiting for', path)
        sleep(5)
    
        resp = requests.get('http://{}{}'.format(instance_dns_name, path))
    
        if resp.status_code == 200:
            break

    return signer.unsign(resp.content)

def add_permanent_github_deploy_key(deploy_key, reponame, auth):
    ''' Add a new repository deploy key.

        https://developer.github.com/v3/repos/keys/#create
    '''
    key_name = 'chimecms-key'
    keys_url = urljoin(GITHUB_API_BASE, '/repos/chimecms/{}/keys'.format(reponame))
    head = {'Content-Type': 'application/json'}
    body = json.dumps(dict(title=key_name, key=deploy_key))
    resp = requests.post(keys_url, body, headers=head, auth=auth)
    code = resp.status_code

    if code == 422:
        # Github deploy key already exists, but likely to be tied to OAuth token.
        # Delete it, and recreate with basic auth so it survives auth deletion.
        resp = requests.get(keys_url, auth=auth)
        key_url = [k['url'] for k in resp.json() if k['title'] == 'token-key'][0]
        resp = requests.delete(key_url, auth=auth)
        code = resp.status_code
    
        if code not in range(200, 299):
            raise RuntimeError('Github deploy key deletion failed, status {}'.format(code))
    
        print('    Deleted temporary token key')
        resp = requests.post(keys_url, body, headers=head, auth=auth)
        code = resp.status_code
    
        if code not in range(200, 299):
            raise RuntimeError('Github deploy key recreation failed, status {}'.format(code))
    
    elif code not in range(200, 299):
        raise RuntimeError('Github deploy key creation failed, status {}'.format(code))

    print('--> Created permanent deploy key', key_name)

def delete_temporary_github_authorization(github_auth_id, auth):
    ''' Delete Github authorization.

        https://developer.github.com/v3/oauth_authorizations/#delete-an-authorization
    '''
    url = urljoin(GITHUB_API_BASE, '/authorizations/{}'.format(github_auth_id))
    resp = requests.delete(url, auth=auth)

    check_status(resp, 'delete authorization {}'.format(github_auth_id))
    
    print('--> Deleted temporary Github token')

def create_cname_record(route53, reponame, cname_value):
    ''' Write domain name to Route 53.
    '''
    cname = '{reponame}.ceviche.chimecms.org'.format(**locals())

    zone = route53.get_zone('chimecms.org')
    zone.add_record('CNAME', cname, cname_value, 60)
    
    print('--> Prepared DNS name', cname)

    return cname

def save_details(credentials, name, cname, instance, reponame, sheet_url, deploy_key):
    '''
    '''
    print('    Preparing details for instances spreadsheet')

    chimecms_url = 'http://{}'.format(cname)
    instance_query = 'region={}#Instances:instanceId={}'.format(instance.region.name, instance.id)
    instance_url = 'https://console.aws.amazon.com/ec2/v2/home?{}'.format(instance_query)
    github_url = 'https://github.com/chimecms/{}'.format(reponame)
    
    source_id = '1ODc62B7clyNMzwRtpOeqDupsDdaomtfZK-Z_GX0CM90'
    gc = gspread.authorize(credentials)
    doc = gc.open_by_key(source_id)
    sheet = doc.worksheet('Instances')

    new_row = [str(datetime.utcnow()), name,
               chimecms_url, instance_url, github_url, sheet_url, deploy_key]

    sheet.append_row(new_row)

    print('--> Wrote details to instances spreadsheet')
