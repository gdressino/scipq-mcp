package javax.jdo.annotations;

public @interface Column {
    String name() default "";

    int length() default -1;

    String allowsNull() default "";
}
