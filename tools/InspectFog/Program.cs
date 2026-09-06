using System.Reflection;
using Il2Cpp;
var t = typeof(Panel_Map);
foreach (var p in t.GetProperties(BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic))
  if (p.Name.Contains("Fog")) Console.WriteLine($"PROP {p.PropertyType} {p.Name}");
foreach (var f in t.GetFields(BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic))
  if (f.Name.Contains("Fog") && !f.Name.Contains("NativeMethod")) Console.WriteLine($"FIELD {f.FieldType} {f.Name.Replace("NativeFieldInfoPtr_","")}");
