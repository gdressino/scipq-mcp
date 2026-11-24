package com.example.shop;

import java.util.List;
import java.util.Map;

/** Overloads whose one without parameters is not SCIP's first. */
public class Overloaded {
    public int pick(int n) {
        return n;
    }

    public int pick() {
        return pick(0);
    }

    public int pick(String s, int n) {
        return s.length() + n;
    }

    public int pick(Map<String, List<Integer>> m, String... rest) {
        return m.size() + rest.length;
    }

    public <T> int pick(T value, long n) {
        return (int) n;
    }

    int use() {
        return pick() + pick(1) + pick("a", 2) + pick(Map.of()) + pick(this, 3L);
    }
}
