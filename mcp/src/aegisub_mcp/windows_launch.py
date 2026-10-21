"""Launch an explicitly requested GUI outside a client's kill-on-close job."""
import os
import subprocess


def launch_through_desktop(command: list[str]) -> None:
    # The local WMI process provider creates the launcher independently of the
    # MCP client's kill-on-close job. Use the caller's normal user token and
    # interactive desktop; no elevation or security setting changes are used.
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    service = startup = process = parameters = result = None
    try:
        service = win32com.client.Dispatch('WbemScripting.SWbemLocator').ConnectServer('.', r'root\cimv2')
        service.Security_.ImpersonationLevel = 3
        startup = service.Get('Win32_ProcessStartup').SpawnInstance_()
        startup.ShowWindow = 0
        startup.WinstationDesktop = r'winsta0\default'
        startup.CreateFlags = 1024  # CREATE_UNICODE_ENVIRONMENT
        names = {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE',
                 'APPDATA', 'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH', 'LANG'}
        startup.EnvironmentVariables = [f'{key}={value}' for key, value in os.environ.items() if key.upper() in names]
        process = service.Get('Win32_Process')
        parameters = process.Methods_('Create').InParameters.SpawnInstance_()
        parameters.CommandLine = subprocess.list2cmdline(command)
        parameters.ProcessStartupInformation = startup
        result = process.ExecMethod_('Create', parameters)
        code = result.Properties_('ReturnValue').Value
        if code != 0:
            raise RuntimeError(f'Windows could not launch the requested window (process provider code {code})')
    finally:
        # Release COM proxies while the apartment is still initialized.
        result = parameters = process = startup = service = None
        pythoncom.CoUninitialize()
