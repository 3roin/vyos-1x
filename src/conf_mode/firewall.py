#!/usr/bin/env python3
#
# Copyright (C) 2021 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import re

from glob import glob
from json import loads
from sys import exit

from vyos.base import Warning
from vyos.config import Config
from vyos.configdict import dict_merge
from vyos.configdict import node_changed
from vyos.configdiff import get_config_diff, Diff
from vyos.template import render
from vyos.util import cmd
from vyos.util import dict_search_args
from vyos.util import process_named_running
from vyos.util import run
from vyos.xml import defaults
from vyos import ConfigError
from vyos import airbag
airbag.enable()

policy_route_conf_script = '/usr/libexec/vyos/conf_mode/policy-route.py'

nftables_conf = '/run/nftables.conf'
nftables_defines_conf = '/run/nftables_defines.conf'

sysfs_config = {
    'all_ping': {'sysfs': '/proc/sys/net/ipv4/icmp_echo_ignore_all', 'enable': '0', 'disable': '1'},
    'broadcast_ping': {'sysfs': '/proc/sys/net/ipv4/icmp_echo_ignore_broadcasts', 'enable': '0', 'disable': '1'},
    'ip_src_route': {'sysfs': '/proc/sys/net/ipv4/conf/*/accept_source_route'},
    'ipv6_receive_redirects': {'sysfs': '/proc/sys/net/ipv6/conf/*/accept_redirects'},
    'ipv6_src_route': {'sysfs': '/proc/sys/net/ipv6/conf/*/accept_source_route', 'enable': '0', 'disable': '-1'},
    'log_martians': {'sysfs': '/proc/sys/net/ipv4/conf/all/log_martians'},
    'receive_redirects': {'sysfs': '/proc/sys/net/ipv4/conf/*/accept_redirects'},
    'send_redirects': {'sysfs': '/proc/sys/net/ipv4/conf/*/send_redirects'},
    'source_validation': {'sysfs': '/proc/sys/net/ipv4/conf/*/rp_filter', 'disable': '0', 'strict': '1', 'loose': '2'},
    'syn_cookies': {'sysfs': '/proc/sys/net/ipv4/tcp_syncookies'},
    'twa_hazards_protection': {'sysfs': '/proc/sys/net/ipv4/tcp_rfc1337'}
}

NAME_PREFIX = 'NAME_'
NAME6_PREFIX = 'NAME6_'

preserve_chains = [
    'INPUT',
    'FORWARD',
    'OUTPUT',
    'VYOS_FW_FORWARD',
    'VYOS_FW_LOCAL',
    'VYOS_FW_OUTPUT',
    'VYOS_POST_FW',
    'VYOS_FRAG_MARK',
    'VYOS_FW6_FORWARD',
    'VYOS_FW6_LOCAL',
    'VYOS_FW6_OUTPUT',
    'VYOS_POST_FW6',
    'VYOS_FRAG6_MARK'
]

nft_iface_chains = ['VYOS_FW_FORWARD', 'VYOS_FW_OUTPUT', 'VYOS_FW_LOCAL']
nft6_iface_chains = ['VYOS_FW6_FORWARD', 'VYOS_FW6_OUTPUT', 'VYOS_FW6_LOCAL']

valid_groups = [
    'address_group',
    'network_group',
    'port_group'
]

snmp_change_type = {
    'unknown': 0,
    'add': 1,
    'delete': 2,
    'change': 3
}
snmp_event_source = 1
snmp_trap_mib = 'VYATTA-TRAP-MIB'
snmp_trap_name = 'mgmtEventTrap'

def get_firewall_interfaces(conf):
    out = {}
    interfaces = conf.get_config_dict(['interfaces'], key_mangling=('-', '_'), get_first_key=True,
                                    no_tag_node_value_mangle=True)
    def find_interfaces(iftype_conf, output={}, prefix=''):
        for ifname, if_conf in iftype_conf.items():
            if 'firewall' in if_conf:
                output[prefix + ifname] = if_conf['firewall']
            for vif in ['vif', 'vif_s', 'vif_c']:
                if vif in if_conf:
                    output.update(find_interfaces(if_conf[vif], output, f'{prefix}{ifname}.'))
        return output
    for iftype, iftype_conf in interfaces.items():
        out.update(find_interfaces(iftype_conf))
    return out

def get_firewall_zones(conf):
    used_v4 = []
    used_v6 = []
    zone_policy = conf.get_config_dict(['zone-policy'], key_mangling=('-', '_'), get_first_key=True,
                                    no_tag_node_value_mangle=True)

    if 'zone' in zone_policy:
        for zone, zone_conf in zone_policy['zone'].items():
            if 'from' in zone_conf:
                for from_zone, from_conf in zone_conf['from'].items():
                    name = dict_search_args(from_conf, 'firewall', 'name')
                    if name:
                        used_v4.append(name)

                    ipv6_name = dict_search_args(from_conf, 'firewall', 'ipv6_name')
                    if ipv6_name:
                        used_v6.append(ipv6_name)

            if 'intra_zone_filtering' in zone_conf:
                name = dict_search_args(zone_conf, 'intra_zone_filtering', 'firewall', 'name')
                if name:
                    used_v4.append(name)

                ipv6_name = dict_search_args(zone_conf, 'intra_zone_filtering', 'firewall', 'ipv6_name')
                if ipv6_name:
                    used_v6.append(ipv6_name)

    return {'name': used_v4, 'ipv6_name': used_v6}

def get_config(config=None):
    if config:
        conf = config
    else:
        conf = Config()
    base = ['firewall']

    firewall = conf.get_config_dict(base, key_mangling=('-', '_'), get_first_key=True,
                                    no_tag_node_value_mangle=True)

    default_values = defaults(base)
    firewall = dict_merge(default_values, firewall)

    firewall['policy_resync'] = bool('group' in firewall or node_changed(conf, base + ['group']))
    firewall['interfaces'] = get_firewall_interfaces(conf)
    firewall['zone_policy'] = get_firewall_zones(conf)

    if 'config_trap' in firewall and firewall['config_trap'] == 'enable':
        diff = get_config_diff(conf)
        firewall['trap_diff'] = diff.get_child_nodes_diff_str(base)
        firewall['trap_targets'] = conf.get_config_dict(['service', 'snmp', 'trap-target'],
                                        key_mangling=('-', '_'), get_first_key=True,
                                        no_tag_node_value_mangle=True)

    return firewall

def verify_rule(firewall, rule_conf, ipv6):
    if 'action' not in rule_conf:
        raise ConfigError('Rule action must be defined')

    if 'fragment' in rule_conf:
        if {'match_frag', 'match_non_frag'} <= set(rule_conf['fragment']):
            raise ConfigError('Cannot specify both "match-frag" and "match-non-frag"')

    if 'limit' in rule_conf:
        if 'rate' in rule_conf['limit']:
            rate_int = re.sub(r'\D', '', rule_conf['limit']['rate'])
            if int(rate_int) < 1:
                raise ConfigError('Limit rate integer cannot be less than 1')

    if 'ipsec' in rule_conf:
        if {'match_ipsec', 'match_non_ipsec'} <= set(rule_conf['ipsec']):
            raise ConfigError('Cannot specify both "match-ipsec" and "match-non-ipsec"')

    if 'recent' in rule_conf:
        if not {'count', 'time'} <= set(rule_conf['recent']):
            raise ConfigError('Recent "count" and "time" values must be defined')

    tcp_flags = dict_search_args(rule_conf, 'tcp', 'flags')
    if tcp_flags:
        if dict_search_args(rule_conf, 'protocol') != 'tcp':
            raise ConfigError('Protocol must be tcp when specifying tcp flags')

        not_flags = dict_search_args(rule_conf, 'tcp', 'flags', 'not')
        if not_flags:
            duplicates = [flag for flag in tcp_flags if flag in not_flags]
            if duplicates:
                raise ConfigError(f'Cannot match a tcp flag as set and not set')

    if 'protocol' in rule_conf:
        if rule_conf['protocol'] == 'icmp' and ipv6:
            raise ConfigError(f'Cannot match IPv4 ICMP protocol on IPv6, use ipv6-icmp')
        if rule_conf['protocol'] == 'ipv6-icmp' and not ipv6:
            raise ConfigError(f'Cannot match IPv6 ICMP protocol on IPv4, use icmp')

    for side in ['destination', 'source']:
        if side in rule_conf:
            side_conf = rule_conf[side]

            if 'group' in side_conf:
                if {'address_group', 'network_group'} <= set(side_conf['group']):
                    raise ConfigError('Only one address-group or network-group can be specified')

                for group in valid_groups:
                    if group in side_conf['group']:
                        group_name = side_conf['group'][group]

                        if group_name and group_name[0] == '!':
                            group_name = group_name[1:]

                        fw_group = f'ipv6_{group}' if ipv6 and group in ['address_group', 'network_group'] else group
                        error_group = fw_group.replace("_", "-")
                        group_obj = dict_search_args(firewall, 'group', fw_group, group_name)

                        if group_obj is None:
                            raise ConfigError(f'Invalid {error_group} "{group_name}" on firewall rule')

                        if not group_obj:
                            Warning(f'{error_group} "{group_name}" has no members!')

            if 'port' in side_conf or dict_search_args(side_conf, 'group', 'port_group'):
                if 'protocol' not in rule_conf:
                    raise ConfigError('Protocol must be defined if specifying a port or port-group')

                if rule_conf['protocol'] not in ['tcp', 'udp', 'tcp_udp']:
                    raise ConfigError('Protocol must be tcp, udp, or tcp_udp when specifying a port or port-group')

def verify(firewall):
    if 'config_trap' in firewall and firewall['config_trap'] == 'enable':
        if not firewall['trap_targets']:
            raise ConfigError(f'Firewall config-trap enabled but "service snmp trap-target" is not defined')

    for name in ['name', 'ipv6_name']:
        if name in firewall:
            for name_id, name_conf in firewall[name].items():
                if name_id in preserve_chains:
                    raise ConfigError(f'Firewall name "{name_id}" is reserved for VyOS')

                if name_id.startswith("VZONE"):
                    raise ConfigError(f'Firewall name "{name_id}" uses reserved prefix')

                if 'rule' in name_conf:
                    for rule_id, rule_conf in name_conf['rule'].items():
                        verify_rule(firewall, rule_conf, name == 'ipv6_name')

    for ifname, if_firewall in firewall['interfaces'].items():
        for direction in ['in', 'out', 'local']:
            name = dict_search_args(if_firewall, direction, 'name')
            ipv6_name = dict_search_args(if_firewall, direction, 'ipv6_name')

            if name and dict_search_args(firewall, 'name', name) == None:
                raise ConfigError(f'Firewall name "{name}" is still referenced on interface {ifname}')

            if ipv6_name and dict_search_args(firewall, 'ipv6_name', ipv6_name) == None:
                raise ConfigError(f'Firewall ipv6-name "{ipv6_name}" is still referenced on interface {ifname}')

    for fw_name, used_names in firewall['zone_policy'].items():
        for name in used_names:
            if dict_search_args(firewall, fw_name, name) == None:
                raise ConfigError(f'Firewall {fw_name.replace("_", "-")} "{name}" is still referenced in zone-policy')

    return None

def cleanup_rule(table, jump_chain):
    commands = []
    chains = nft_iface_chains if table == 'ip filter' else nft6_iface_chains
    for chain in chains:
        results = cmd(f'nft -a list chain {table} {chain}').split("\n")
        for line in results:
            if f'jump {jump_chain}' in line:
                handle_search = re.search('handle (\d+)', line)
                if handle_search:
                    commands.append(f'delete rule {table} {chain} handle {handle_search[1]}')
    return commands

def cleanup_commands(firewall):
    commands = []
    commands_end = []
    for table in ['ip filter', 'ip6 filter']:
        state_chain = 'VYOS_STATE_POLICY' if table == 'ip filter' else 'VYOS_STATE_POLICY6'
        json_str = cmd(f'nft -j list table {table}')
        obj = loads(json_str)
        if 'nftables' not in obj:
            continue
        for item in obj['nftables']:
            if 'chain' in item:
                chain = item['chain']['name']
                if chain in ['VYOS_STATE_POLICY', 'VYOS_STATE_POLICY6']:
                    if 'state_policy' not in firewall:
                        commands.append(f'delete chain {table} {chain}')
                    else:
                        commands.append(f'flush chain {table} {chain}')
                elif chain not in preserve_chains and not chain.startswith("VZONE"):
                    if table == 'ip filter' and dict_search_args(firewall, 'name', chain.replace(NAME_PREFIX, "", 1)) != None:
                        commands.append(f'flush chain {table} {chain}')
                    elif table == 'ip6 filter' and dict_search_args(firewall, 'ipv6_name', chain.replace(NAME6_PREFIX, "", 1)) != None:
                        commands.append(f'flush chain {table} {chain}')
                    else:
                        commands += cleanup_rule(table, chain)
                        commands.append(f'delete chain {table} {chain}')
            elif 'rule' in item:
                rule = item['rule']
                if rule['chain'] in ['VYOS_FW_FORWARD', 'VYOS_FW_OUTPUT', 'VYOS_FW_LOCAL', 'VYOS_FW6_FORWARD', 'VYOS_FW6_OUTPUT', 'VYOS_FW6_LOCAL']:
                    if 'expr' in rule and any([True for expr in rule['expr'] if dict_search_args(expr, 'jump', 'target') == state_chain]):
                        if 'state_policy' not in firewall:
                            chain = rule['chain']
                            handle = rule['handle']
                            commands.append(f'delete rule {table} {chain} handle {handle}')
            elif 'set' in item:
                set_name = item['set']['name']
                commands_end.append(f'delete set {table} {set_name}')
    return commands + commands_end

def generate(firewall):
    if not os.path.exists(nftables_conf):
        firewall['first_install'] = True
    else:
        firewall['cleanup_commands'] = cleanup_commands(firewall)

    render(nftables_conf, 'firewall/nftables.j2', firewall)
    render(nftables_defines_conf, 'firewall/nftables-defines.j2', firewall)
    return None

def apply_sysfs(firewall):
    for name, conf in sysfs_config.items():
        paths = glob(conf['sysfs'])
        value = None

        if name in firewall:
            conf_value = firewall[name]

            if conf_value in conf:
                value = conf[conf_value]
            elif conf_value == 'enable':
                value = '1'
            elif conf_value == 'disable':
                value = '0'

        if value:
            for path in paths:
                with open(path, 'w') as f:
                    f.write(value)

def post_apply_trap(firewall):
    if 'first_install' in firewall:
        return None

    if 'config_trap' not in firewall or firewall['config_trap'] != 'enable':
        return None

    if not process_named_running('snmpd'):
        return None

    trap_username = os.getlogin()

    for host, target_conf in firewall['trap_targets'].items():
        community = target_conf['community'] if 'community' in target_conf else 'public'
        port = int(target_conf['port']) if 'port' in target_conf else 162

        base_cmd = f'snmptrap -v2c -c {community} {host}:{port} 0 {snmp_trap_mib}::{snmp_trap_name} '

        for change_type, changes in firewall['trap_diff'].items():
            for path_str, value in changes.items():
                objects = [
                    f'mgmtEventUser s "{trap_username}"',
                    f'mgmtEventSource i {snmp_event_source}',
                    f'mgmtEventType i {snmp_change_type[change_type]}'
                ]

                if change_type == 'add':
                    objects.append(f'mgmtEventCurrCfg s "{path_str} {value}"')
                elif change_type == 'delete':
                    objects.append(f'mgmtEventPrevCfg s "{path_str} {value}"')
                elif change_type == 'change':
                    objects.append(f'mgmtEventPrevCfg s "{path_str} {value[0]}"')
                    objects.append(f'mgmtEventCurrCfg s "{path_str} {value[1]}"')

                cmd(base_cmd + ' '.join(objects))

def state_policy_rule_exists():
    # Determine if state policy rules already exist in nft
    search_str = cmd(f'nft list chain ip filter VYOS_FW_FORWARD')
    return 'VYOS_STATE_POLICY' in search_str

def resync_policy_route():
    # Update policy route as firewall groups were updated
    tmp = run(policy_route_conf_script)
    if tmp > 0:
        Warning('Failed to re-apply policy route configuration!')

def apply(firewall):
    if 'first_install' in firewall:
        run('nfct helper add rpc inet tcp')
        run('nfct helper add rpc inet udp')
        run('nfct helper add tns inet tcp')

    install_result = run(f'nft -f {nftables_conf}')
    if install_result == 1:
        raise ConfigError('Failed to apply firewall')

    if 'state_policy' in firewall and not state_policy_rule_exists():
        for chain in ['VYOS_FW_FORWARD', 'VYOS_FW_OUTPUT', 'VYOS_FW_LOCAL']:
            cmd(f'nft insert rule ip filter {chain} jump VYOS_STATE_POLICY')

        for chain in ['VYOS_FW6_FORWARD', 'VYOS_FW6_OUTPUT', 'VYOS_FW6_LOCAL']:
            cmd(f'nft insert rule ip6 filter {chain} jump VYOS_STATE_POLICY6')

    apply_sysfs(firewall)

    if firewall['policy_resync']:
        resync_policy_route()

    post_apply_trap(firewall)

    return None

if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
