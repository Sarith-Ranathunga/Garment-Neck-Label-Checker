# Minification is off for the release build (see app/build.gradle.kts), so
# these rules are not currently applied. They are here so that turning it on
# later does not silently break Room's generated code or Retrofit's
# reflection over the DTO classes — the two things that fail at runtime rather
# than at build time, and only on the release build.

-keep class lk.ac.nibm.gnlc.net.** { *; }
-keep class lk.ac.nibm.gnlc.data.*Entity { *; }
-keepattributes Signature, *Annotation*

# Retrofit's interface methods are read reflectively.
-keep,allowobfuscation interface lk.ac.nibm.gnlc.net.Api
-keepclassmembers,allowobfuscation class * {
  @com.google.gson.annotations.SerializedName <fields>;
}
