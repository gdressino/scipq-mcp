package com.example.shop;

/**
 * Overloads in links: {@link #m()} and {@link #m(String)}; types: {@link Bar} and {@link FooBar}.
 */
public class Units {
    public int m() {
        return 0;
    }

    public int m(String s) {
        return s.length();
    }

    int use(Bar b, FooBar f) {
        return m() + m("x");
    }
}
