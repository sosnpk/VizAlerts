#! python
# -*- coding: utf-8 -*-
# This is a utility module for integrating email functionality into VizAlerts.

import smtplib
import re
import os.path
from email.encoders import encode_base64

# added for MIME handling
from itertools import chain
from errno import ECONNREFUSED
from mimetypes import guess_type
from subprocess import Popen, PIPE

from cStringIO import StringIO
from email.header import Header
from email import Charset
from email.generator import Generator
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from socket import error as SocketError

# import local modules
import config
import log
import vizalert

# regular expression used to split recipient address strings into separate email addresses
EMAIL_RECIP_SPLIT_REGEX = u'[; ,]*'


def send_email(fromaddr, toaddrs, subject, content, ccaddrs=None, bccaddrs=None, inlineattachments=None,
               appendattachments=None):
    """Generic function to send an email. The presumption is that all arguments have been validated prior to the call
        to this function.

    Input arguments are:
        fromaddr    single email address
        toaddr      string of recipient email addresses separated by the list of separators in EMAIL_RECIP_SPLIT_REGEX
        subject     string that is subject of email
        content     body of email, may contain HTML
        ccaddrs     cc recipients, see toaddr
        bccaddrs    bcc recipients, see toaddr
        inlineattachments   List of vizref dicts where each dict has one attachment. The minimum dict has an
                            imagepath key that points to the file to be attached.
        appendattachments   Appended (non-inline attachments). See inlineattachments for details on structure.

    Nothing is returned by this function unless there is an exception.

    """
    try:
        log.logger.info(
            u'sending email: {},{},{},{},{},{},{}'.format(config.configs['smtp.serv'], fromaddr, toaddrs, ccaddrs, bccaddrs,
                                                          subject, inlineattachments, appendattachments))
        log.logger.debug(u'email body: {}'.format(content))

        # using mixed type because there can be inline and non-inline attachments
        msg = MIMEMultipart('mixed')
        msg.set_charset('utf-8')
        msg.preamble = subject.encode('utf-8')
        msg['From'] = Header(fromaddr)
        msg['Subject'] = Header(subject.encode('utf-8'), 'UTF-8').encode()

        # Process direct recipients
        toaddrs = re.split(EMAIL_RECIP_SPLIT_REGEX, toaddrs.strip())
        msg['To'] = Header(', '.join(toaddrs))
        allrecips = toaddrs

        # Process indirect recipients
        if ccaddrs:
            ccaddrs = re.split(EMAIL_RECIP_SPLIT_REGEX, ccaddrs.strip())
            msg['CC'] = Header(', '.join(ccaddrs))
            allrecips.extend(ccaddrs)

        if bccaddrs:
            bccaddrs = re.split(EMAIL_RECIP_SPLIT_REGEX, bccaddrs.strip())
            # don't add to header, they are blind carbon-copied
            allrecips.extend(bccaddrs)

        # Create a section for the body and inline attachments
        msgalternative = MIMEMultipart(u'related')
        msg.attach(msgalternative)
        msgalternative.attach(MIMEText(content.encode('utf-8'), 'html', 'utf-8'))

        # Add inline attachments
        if inlineattachments != None:
            for vizref in inlineattachments:
                msgalternative.attach(mimify_file(vizref['imagepath'], inline=True))

        # Add appended attachments from Email Attachments field and prevent dup custom filenames
        #  MC: Feels like this code should be in VizAlert class? Or module? Not sure, leaving it here for now
        appendedfilenames = []
        if appendattachments != None:
            appendattachments = vizalert.merge_pdf_attachments(appendattachments)
            for vizref in appendattachments:
                # if there is no |filename= option set then use the exported imagepath
                if 'filename' not in vizref:
                    msg.attach(mimify_file(vizref['imagepath'], inline=False))
                else:
                    # we need to make sure the custom filename is unique, if so then
                    # use the custom filename
                    if vizref['filename'] not in appendedfilenames:
                        appendedfilenames.append(vizref['filename'])
                        msg.attach(mimify_file(vizref['imagepath'], inline=False, overridename=vizref['filename']))
                    # use the exported imagepath
                    else:
                        msg.attach(mimify_file(vizref['imagepath'], inline=False))
                        log.logger.info(u'Warning: attempted to attach duplicate filename ' + vizref[
                            'filename'] + ', using unique auto-generated name instead.')

        server = smtplib.SMTP(config.configs['smtp.serv'], config.configs['smtp.port'])
        if config.configs['smtp.ssl']:
            server.ehlo()
            server.starttls()
        if config.configs['smtp.user']:
            server.login(str(config.configs['smtp.user']), str(config.configs['smtp.password']))

        # from http://wordeology.com/computer/how-to-send-good-unicode-email-with-python.html
        io = StringIO()
        g = Generator(io, False)  # second argument means "should I mangle From?"
        g.flatten(msg)

        server.sendmail(fromaddr.encode('utf-8'), [addr.encode('utf-8') for addr in allrecips], io.getvalue())
        server.quit()
    except smtplib.SMTPConnectError as e:
        log.logger.error(u'Email failed to send; there was an issue connecting to the SMTP server: {}'.format(e))
        raise e
    except smtplib.SMTPHeloError as e:
        log.logger.error(u'Email failed to send; the SMTP server refused our HELO message: {}'.format(e))
        raise e
    except smtplib.SMTPAuthenticationError as e:
        log.logger.error(u'Email failed to send; there was an issue authenticating to SMTP server: {}'.format(e))
        raise e
    except smtplib.SMTPException as e:
        log.logger.error(u'Email failed to send; there was an issue sending mail via SMTP server: {}'.format(e))
        raise e
    except Exception as e:
        log.logger.error(u'Email failed to send: {}'.format(e))
        raise e
        

def addresses_are_invalid(emailaddresses, emptystringok, regex_eval=None):
    """Validates all email addresses found in a given string, optionally that conform to the regex_eval"""
    log.logger.debug(u'Validating email field value: {}'.format(emailaddresses))
    address_list = re.split(EMAIL_RECIP_SPLIT_REGEX, emailaddresses.strip())
    for address in address_list:
        log.logger.debug(u'Validating presumed email address: {}'.format(address))
        if emptystringok and (address == '' or address is None):
            return None
        else:
            errormessage = address_is_invalid(address, regex_eval)
            if errormessage:
                log.logger.debug(u'Address is invalid: {}, Error: {}'.format(address, errormessage))
                if len(address) > 64:
                    address = address[:64] + '...'  # truncate a too-long address for error formattting purposes
                return {'address': address, 'errormessage': errormessage}
    return None


def address_is_invalid(address, regex_eval=None):
    """Checks for a syntactically invalid email address."""
    # (most code derived from from http://zeth.net/archive/2008/05/03/email-syntax-check)

    # Email address must not be empty
    if address is None or len(address) == 0 or address == '':
        errormessage = u'Address is empty'
        log.logger.error(errormessage)
        return errormessage

    # Validate address according to admin regex
    if regex_eval:
        log.logger.debug("testing address {} against regex {}".format(address, regex_eval))
        if not re.match(regex_eval, address):
            errormessage = u'Address must match regex pattern set by the administrator: {}'.format(regex_eval)
            log.logger.error(errormessage)
            return errormessage

    # Email address must be 6 characters in total.
    # This is not an RFC defined rule but is easy
    if len(address) < 6:
        errormessage = u'Address is too short: {}'.format(address)
        log.logger.error(errormessage)
        return errormessage

    # Unicode in addresses not yet supported
    try:
        address.decode('ascii')
    except Exception as e:
        errormessage = u'Address must contain only ASCII characers: {}'.format(address)
        log.logger.error(errormessage)
        return errormessage

    # Split up email address into parts.
    try:
        localpart, domainname = address.rsplit('@', 1)
        host, toplevel = domainname.rsplit('.', 1)
        log.logger.debug(u'Splitting Address: localpart, domainname, host, toplevel: {},{},{},{}'.format(localpart,
                                                                                                     domainname,
                                                                                                     host,
                                                                                                     toplevel))
    except ValueError:
        errormessage = u'Address has too few parts'
        log.logger.error(errormessage)
        return errormessage

    for i in '-_.%+.':
        localpart = localpart.replace(i, "")
    for i in '-_.':
        host = host.replace(i, "")

    log.logger.debug(u'Removing other characters from address: localpart, host: {},{}'.format(localpart, host))

    # check for length
    if len(localpart) > 64:
        errormessage = u'Localpart of address exceeds max length (65 characters)'
        log.logger.error(errormessage)
        return errormessage

    if len(address) > 254:
        errormessage = u'Address exceeds max length (254 characters)'
        log.logger.error(errormessage)
        return errormessage

    if localpart.isalnum() and host.isalnum():
        return None  # Email address is fine.
    else:
        errormessage = u'Address has funny characters'
        log.logger.error(errormessage)
        return errormessage


def mimify_file(filename, inline=True, overridename=None):
    """Returns an appropriate MIME object for the given file.

    :param filename: A valid path to a file
    :type filename: str

    :returns: A MIME object for the given file
    :rtype: instance of MIMEBase
    """

    filename = os.path.abspath(os.path.expanduser(filename))
    if overridename:
        basefilename = overridename
    else:
        basefilename = os.path.basename(filename)

    if inline:
        msg = MIMEBase(*get_mimetype(filename))
        msg.set_payload(open(filename, "rb").read())
        msg.add_header('Content-ID', '<{}>'.format(basefilename))
        msg.add_header('Content-Disposition', 'inline; filename="%s"' % basefilename)
    else:
        msg = MIMEBase(*get_mimetype(filename))
        msg.set_payload(open(filename, "rb").read())
        if overridename:
            basefilename = overridename

        msg.add_header('Content-Disposition', 'attachment; filename="%s"' % basefilename)

    encode_base64(msg)
    return msg


def get_mimetype(filename):
    """Returns the MIME type of the given file.

    :param filename: A valid path to a file
    :type filename: str

    :returns: The file's MIME type
    :rtype: tuple
    """
    content_type, encoding = guess_type(filename)
    if content_type is None or encoding is not None:
        content_type = "application/octet-stream"
    return content_type.split("/", 1)


def validate_addresses(vizdata,
                       allowed_from_address,
                       allowed_recipient_addresses,
                       email_to_field,
                       email_from_field,
                       email_cc_field,
                       email_bcc_field):
    """Loops through the viz data for an Advanced Alert and returns a list of dicts
        containing any errors found in recipients"""

    errorlist = []
    rownum = 2  # account for field header in CSV

    for row in vizdata:
        result = addresses_are_invalid(row[email_to_field], False,
                                       allowed_recipient_addresses)  # empty string not acceptable as a To address
        if result:
            errorlist.append(
                {'Row': rownum, 'Field': email_to_field, 'Value': result['address'], 'Error': result['errormessage']})
        if email_from_field:
            result = addresses_are_invalid(row[email_from_field], False,
                                           allowed_from_address)  # empty string not acceptable as a From address
            if result:
                errorlist.append({'Row': rownum, 'Field': email_from_field, 'Value': result['address'],
                                  'Error': result['errormessage']})
        if email_cc_field:
            result = addresses_are_invalid(row[email_cc_field], True, allowed_recipient_addresses)
            if result:
                errorlist.append({'Row': rownum, 'Field': email_cc_field, 'Value': result['address'],
                                  'Error': result['errormessage']})
        if email_bcc_field:
            result = addresses_are_invalid(row[email_bcc_field], True, allowed_recipient_addresses)
            if result:
                errorlist.append({'Row': rownum, 'Field': email_bcc_field, 'Value': result['address'],
                                  'Error': result['errormessage']})
        rownum += 1

    return errorlist
