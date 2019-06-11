#!/usr/bin/env python3

#Refs:
#    https://bitbucket.org/paulc/dnslib/src/default/dnslib/intercept.py

import binascii
import copy
import socket
import struct
import sys
import boto3

from dnslib import DNSRecord,RR,QTYPE,RCODE,parse_time
from dnslib.server import DNSServer,DNSHandler,BaseResolver,DNSLogger
from dnslib.label import DNSLabel


class InterceptResolver(BaseResolver):
    def __init__(self,address,port,ttl,timeout=0):
        """
            address/port    - upstream server
            ttl             - default ttl for intercept records
            timeout         - timeout for upstream server
        """
        self.address = address
        self.port = port
        self.ttl = parse_time(ttl)
        self.timeout = timeout

    def resolve(self,request,handler):
        reply = request.reply()
        qname = request.q.qname
        qtype = QTYPE[request.q.qtype]
        # Try to resolve locally

        rtype = 'A' #Default to returning an A record unless otherwise specified
        rdata = '0.0.0.0' #Default IP to return(what would be best here?)
        if "aws.dns" in str(qname):
            subdomain = str(qname).strip('.').split('.')[:-2]
            service = subdomain.pop()
            if "ec2" in service:
                type = subdomain.pop()
                if "ip" in type:
                    rtype = 'A'

                elif "cname" in type:
                    rtype = 'CNAME'

                net = subdomain.pop()

                #Grab the instance object
                id = subdomain.pop()
                ec2 = boto3.resource('ec2', region_name="us-west-2")    #TODO: some region setting somewhere
                instance = ec2.Instance(id)
                if "private" in net:
                    if "ip" in type:
                        rdata = str(instance.private_ip_address)
                    elif 'cname' in type:
                        rdata = str(instance.private_dns_name)
                elif "public" in net:
                    if "ip" in type:
                        rdata = str(instance.public_ip_address)
                    elif 'cname' in type:
                        rdata = str(instance.public_dns_name)
                    print(rdata)
            #Check we have a reply:
            if rdata == 'None' or rdata == '':
                reply.header.rcode = getattr(RCODE,'NXDOMAIN')
                return reply
            #Build an send the reply
            rr = RR.fromZone("{} IN {} {}".format(str(qname), rtype, rdata))[0]
            reply.add_answer(rr)

        # Otherwise proxy
        if not reply.rr:
            try:
                if handler.protocol == 'udp':
                    proxy_r = request.send(self.address,self.port,
                                    timeout=self.timeout)
                else:
                    proxy_r = request.send(self.address,self.port,
                                    tcp=True,timeout=self.timeout)
                reply = DNSRecord.parse(proxy_r)
            except socket.timeout:
                reply.header.rcode = getattr(RCODE,'NXDOMAIN')

        return reply

if __name__ == '__main__':

    import argparse,sys,time

    p = argparse.ArgumentParser(description="DNS Intercept Proxy")
    p.add_argument("--port","-p",type=int,default=53,
                    metavar="<port>",
                    help="Local proxy port (default:53)")
    p.add_argument("--address","-a",default="",
                    metavar="<address>",
                    help="Local proxy listen address (default:all)")
    p.add_argument("--upstream","-u",default="8.8.8.8:53",
            metavar="<dns server:port>",
                    help="Upstream DNS server:port (default:8.8.8.8:53)")
    p.add_argument("--tcp",action='store_true',default=False,
                    help="TCP proxy (default: UDP only)")
    p.add_argument("--ttl","-t",default="60s",
                    metavar="<ttl>",
                    help="Intercept TTL (default: 60s)")
    p.add_argument("--timeout","-o",type=float,default=5,
                    metavar="<timeout>",
                    help="Upstream timeout (default: 5s)")
    p.add_argument("--log",default="request,reply,truncated,error",
                    help="Log hooks to enable (default: +request,+reply,+truncated,+error,-recv,-send,-data)")
    p.add_argument("--log-prefix",action='store_true',default=False,
                    help="Log prefix (timestamp/handler/resolver) (default: False)")
    args = p.parse_args()

    args.dns,_,args.dns_port = args.upstream.partition(':')
    args.dns_port = int(args.dns_port or 53)

    resolver = InterceptResolver(args.dns,
                                 args.dns_port,
                                 args.ttl,
                                 args.timeout)
    logger = DNSLogger(args.log,args.log_prefix)

    print("Starting Intercept Proxy (%s:%d -> %s:%d) [%s]" % (
                        args.address or "*",args.port,
                        args.dns,args.dns_port,
                        "UDP/TCP" if args.tcp else "UDP"))


    DNSHandler.log = {
        'log_request',      # DNS Request
        'log_reply',        # DNS Response
        'log_truncated',    # Truncated
        'log_error',        # Decoding error
    }

    udp_server = DNSServer(resolver,
                           port=args.port,
                           address=args.address,
                           logger=logger)
    udp_server.start_thread()

    if args.tcp:
        tcp_server = DNSServer(resolver,
                               port=args.port,
                               address=args.address,
                               tcp=True,
                               logger=logger)
        tcp_server.start_thread()

    try:
        while udp_server.isAlive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        udp_server.stop()
        if args.tcp:
            tcp_server.stop()
