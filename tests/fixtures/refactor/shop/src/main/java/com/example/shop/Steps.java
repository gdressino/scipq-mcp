package com.example.shop;

/** A chain of calls: what calls base() from outside is five calls away. */
public class Steps {
    static String first() {
        return second();
    }

    static String second() {
        return third();
    }

    static String third() {
        return fourth();
    }

    static String fourth() {
        return base();
    }

    static String base() {
        return "";
    }
}
