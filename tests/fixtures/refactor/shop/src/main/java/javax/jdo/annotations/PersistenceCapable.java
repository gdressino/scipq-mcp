package javax.jdo.annotations;

/** Stand-in for JDO's annotation (the fixture compiles without the JDO jar). */
public @interface PersistenceCapable {
    String table() default "";
}
