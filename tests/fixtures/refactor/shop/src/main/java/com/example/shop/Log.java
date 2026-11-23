package com.example.shop;

public final class Log {
    private Log() {
    }

    public static String log(String fmt, Object... args) {
        return String.format(fmt, args);
    }

    static String use(Object a, Object b) {
        return log("%s %s", a, b) + log("none");
    }
}
