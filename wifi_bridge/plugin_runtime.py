"""Resolve declarative discovery IDs to existing reviewed implementations."""
from .plugins import demand_plugins
from .vespera_demand import VesperaDemand
from .dwarf_demand import DwarfDemand
from .mount_discovery import ZwoMountDemand, TcpPluginDemand, SynscanDiscovery


def adapters(controller):
    result={}
    for spec in demand_plugins():
        kind=spec['id'];engine=spec['discovery']['engine']
        if engine=='seestar_scan_iscope':continue  # Existing Seestar listener lifecycle.
        if kind=='vespera':adapter=VesperaDemand(controller)
        elif kind=='zwo_mount':adapter=ZwoMountDemand(controller)
        elif engine=='tcp_syn':adapter=TcpPluginDemand(controller,kind=kind)
        elif engine=='dwarf_ping':adapter=DwarfDemand(controller,kind=kind)
        elif engine=='synscan_version':adapter=SynscanDiscovery(controller,kind=kind)
        else:raise ValueError('Unsupported compiled plugin adapter.')
        result[kind]=adapter
    return result
