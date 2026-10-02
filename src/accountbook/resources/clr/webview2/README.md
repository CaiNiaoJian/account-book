# CoreCLR WebView2 assemblies

Official Microsoft.Web.WebView2 NuGet package, version **1.0.3856.49**,
matching the SDK shipped by pinned pywebview **6.2.1**.

Source: https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.3856.49/microsoft.web.webview2.1.0.3856.49.nupkg

Only the two `lib_manual/netcoreapp3.0` assemblies and the original Microsoft
license are included. The native loader remains the one shipped by pywebview.
These replace the upstream `net462` assemblies only when CoreCLR is selected.
No Windows trust policy or Framework remote assembly loading setting is changed.

SHA-256:

| File | SHA-256 |
| --- | --- |
| Original NuGet package | `BC0F76EB911B569838DC4AA8F8D325269B966BEDB592863D26211AEF3A099F1A` |
| Microsoft.Web.WebView2.Core.dll | `8219A3135436C147F8EB02FB831E99596103EEBC18B6D7B794CA93F4C67018A2` |
| Microsoft.Web.WebView2.WinForms.dll | `69D70EF3061A5A973942B56BA0472A71F52A26BA22D58E7CB1D6C25B48ED544B` |

`shell/webview_runtime.py` also adapts pywebview's folder picker to the public
WinForms API. The build includes the original upstream backend source and BSD
license. When upgrading pywebview, review that adapter and SDK version together.
